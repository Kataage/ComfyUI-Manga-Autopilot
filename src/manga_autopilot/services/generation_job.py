"""GenerationLoop: candidate generation + QA + retry orchestration (spec 17.1, 17.5).

This service stitches the smaller pieces together:

1. :class:`CandidateGenerator` - produce ``candidate_count`` seed/prompt
   variations for a panel.
2. A pluggable :class:`GenerationExecutor` - submits prompts to
   ComfyUI/ComfyClient (or a fake in tests) and returns image bytes.
3. :class:`QAScoring` + checkers from :mod:`manga_autopilot.services.qa`
   - score each candidate and select the best.
4. :class:`RetryController` - decide whether to revise the prompt,
   change the seed, or fall back to :class:`FallbackGenerator`.

The loop returns a :class:`GenerationJob` whose ``status`` reflects
whether the run produced a usable image, hit the retry cap, or fell
back to the safe placeholder.  The :class:`GenerationJob` is also
persisted to ``{project_root}/jobs/{job_id}.json`` for replay/audit.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from manga_autopilot.models.job import (
    CandidateImageMeta,
    GenerationJob,
    JobStatus,
    write_job,
)
from manga_autopilot.models.page import PanelPlan
from manga_autopilot.models.panel import PanelRecord
from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.qa import (
    CandidateGenerator,
    CandidateImage,
    CandidateSpec,
    FallbackGenerator,
    PanelGeometry,
    QualityResult,
    RetryAction,
    RetryController,
    quality_result_for,
)

log = logging.getLogger(__name__)


JOBS_SUBDIR = "jobs"
PANELS_SUBDIR = "panels"


@dataclass(frozen=True)
class PanelExecutionRequest:
    """Structured context passed to a :class:`GenerationExecutor`.

    Replaces the individual ``prompt``, ``workflow_id``, ``seed``,
    ``candidate_id`` parameters with a single request object that
    carries project / page / panel / candidate context.
    """

    project_id: str
    page_id: str
    panel_id: str
    candidate_id: str
    prompt: PromptSpec
    workflow_id: str
    seed: int
    attempt_index: int = 0
    width: int | None = None
    height: int | None = None
    output_filename: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_width(self) -> int:
        return self.width or self.prompt.width

    @property
    def effective_height(self) -> int:
        return self.height or self.prompt.height


class GenerationExecutor(Protocol):
    """Pluggable executor for ``GenerationLoop``.

    A real implementation talks to ComfyUI via :class:`ComfyClient`; a
    fake implementation (used in tests) can short-circuit and return
    canned images so the loop can be exercised end-to-end.
    """

    async def submit(
        self,
        request: PanelExecutionRequest,
    ) -> GenerationExecutorResult:
        """Render one candidate.  Raise to signal a hard failure."""


@dataclass
class GenerationExecutorResult:
    """A single render result returned by a :class:`GenerationExecutor`."""

    candidate_id: str
    prompt_id: str
    image: Image.Image
    workflow_id: str = ""


class ExecutorTimeout(RuntimeError):
    """Raised by an executor when a submission timed out.

    ``prompt_id`` lets a strict run reconcile against ComfyUI history before
    resubmitting, so a render that actually finished is adopted rather than
    paid for twice.
    """

    def __init__(self, message: str, *, prompt_id: str = "") -> None:
        super().__init__(message)
        self.prompt_id = prompt_id


@dataclass
class GenerationLoopConfig:
    candidate_count: int = 1
    max_retries: int = 1
    threshold: float = 0.5
    panel_width: int = 512
    panel_height: int = 512
    strict: bool = False
    """Strict Anima semantics: no quality auto-retry, no fallback image."""
    technical_retry_count: int = 1
    """Strict mode only: how many times a technical failure is retried."""


@dataclass
class GenerationOutcome:
    """A simplified view of a finished :class:`GenerationJob`."""

    job: GenerationJob
    selected_image_path: Path | None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class GenerationLoop:
    """Drive a panel through candidate generation -> QA -> retry -> persist."""

    project_root: Path
    config: GenerationLoopConfig = field(default_factory=GenerationLoopConfig)
    candidate_generator: CandidateGenerator = field(default_factory=CandidateGenerator)
    retry_controller: RetryController = field(default_factory=RetryController)
    fallback: FallbackGenerator = field(default_factory=FallbackGenerator)
    image_writer: Callable[[Path, Image.Image], Path] | None = None
    """Optional custom image writer.  Defaults to ``Image.save(..., format='PNG')``."""

    def _panels_dir(self) -> Path:
        d = Path(self.project_root) / "assets" / PANELS_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _jobs_dir(self) -> Path:
        d = Path(self.project_root) / JOBS_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_image(self, image: Image.Image, candidate_id: str) -> Path:
        dest = self._panels_dir() / f"{candidate_id}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.image_writer is not None:
            return Path(self.image_writer(dest, image))
        image.save(dest, format="PNG")
        return dest

    def _set_status(self, job: GenerationJob, status: JobStatus) -> None:
        """Transition ``job`` to ``status`` and bump ``updated_at``.

        The job is not persisted to disk here; :meth:`run` writes the
        final state via :func:`write_job` once the loop exits.  This
        keeps status transitions cheap and avoids partial writes when
        the loop aborts partway through.
        """

        if job.status == status:
            return
        job.status = status
        job.touch()

    # ------------------------------------------------------------------ build
    def _build_candidates(
        self,
        panel: PanelPlan,
        prompt: PromptSpec,
        workflow_id: str,
        panel_id: str = "",
    ) -> list[CandidateImage]:
        spec = CandidateSpec(
            panel_id=panel_id or f"panel_{panel.panel_number:03d}",
            candidate_count=self.config.candidate_count,
            base_seed=prompt.seed or 0,
            prompt=prompt,
            workflow_id=workflow_id,
        )
        return self.candidate_generator.generate(spec)

    async def _submit(
        self,
        executor: GenerationExecutor,
        request: PanelExecutionRequest,
    ) -> GenerationExecutorResult:
        """Submit one candidate.

        Generic runs submit once and let any exception propagate. Strict runs
        first reconcile a timeout against ComfyUI history by prompt id, and then
        retry a technical failure ``technical_retry_count`` times. A quality
        problem is not a technical failure and never reaches this method.
        """
        if not self.config.strict:
            return await executor.submit(request)

        attempts = max(0, self.config.technical_retry_count) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await executor.submit(request)
            except ExecutorTimeout as exc:
                last_error = exc
                recovered = await self._reconcile(executor, exc.prompt_id)
                if recovered is not None:
                    log.info(
                        "adopted already-finished render for prompt %s instead of resubmitting",
                        exc.prompt_id,
                    )
                    return recovered
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
                last_error = exc
            if attempt + 1 < attempts:
                log.warning(
                    "technical failure on %s (%s); retrying once",
                    request.candidate_id,
                    last_error,
                )
        assert last_error is not None
        raise last_error

    async def _reconcile(
        self,
        executor: GenerationExecutor,
        prompt_id: str,
    ) -> GenerationExecutorResult | None:
        """Ask the executor whether `prompt_id` already produced an image."""
        if not prompt_id:
            return None
        reconcile = getattr(executor, "reconcile", None)
        if reconcile is None:
            return None
        return await reconcile(prompt_id)

    def _score(
        self,
        panel: PanelPlan,
        candidate: CandidateImage,
        prompt: PromptSpec,
        threshold: float,
    ) -> QualityResult:
        geometry = PanelGeometry(
            panel_id=candidate.panel_id,
            x=0,
            y=0,
            width=candidate.width,
            height=candidate.height,
        )
        return quality_result_for(
            panel=panel,
            candidate=candidate,
            geometry=geometry,
            threshold=threshold,
        )

    # ------------------------------------------------------------------ run
    async def run(
        self,
        *,
        panel: PanelPlan,
        page_number: int,
        prompt: PromptSpec,
        workflow_id: str,
        executor: GenerationExecutor,
        project_id: str,
        cancel_check: Callable[[], bool] | None = None,
        run_id: str = "",
        panel_id: str = "",
    ) -> GenerationOutcome:
        """Run the loop for a single panel and return the outcome.

        ``executor`` is the only side-effectful dependency.  ``cancel_check``
        is invoked between candidates; returning ``True`` aborts and marks
        the job as :attr:`JobStatus.CANCELLED`.

        ``panel_id`` defaults to ``panel_{panel_number:03d}``, which is unique
        only within a page. A caller that spans pages should pass the panel
        record's own id: otherwise page 2's first panel writes over page 1's
        candidate images, which share the derived id.
        """

        job = GenerationJob(
            project_id=project_id,
            page_number=page_number,
            panel_id=panel_id or f"panel_{panel.panel_number:03d}",
            workflow_id=workflow_id,
            input={
                "positive": prompt.positive,
                "negative": prompt.negative,
                "seed": prompt.seed,
                "width": prompt.width,
                "height": prompt.height,
            },
            max_retries=self.config.max_retries,
        )
        page_id = f"page_{page_number:04d}"
        panel_id = panel_id or f"panel_{panel.panel_number:03d}"
        # PENDING is the implicit default; advance to VALIDATING while we
        # build the candidate list so callers can observe a real transition.
        self._set_status(job, JobStatus.VALIDATING)
        job.started_at = _utc_now_iso()

        try:
            try:
                candidates = self._build_candidates(panel, prompt, workflow_id, panel_id)
            except ValueError as exc:
                if self.config.strict:
                    # Strict Anima surfaces misconfiguration instead of quietly
                    # shipping a placeholder panel.
                    raise
                # ``candidate_count=0`` (or other misconfiguration) is treated
                # as a soft failure: fall back to the safe image so the
                # pipeline never crashes.
                log.warning("candidate generation skipped (%s); using fallback", exc)
                job = self._apply_fallback(panel, job, prompt)
                candidates = []

            self._set_status(job, JobStatus.QUEUED)
            current_prompt = prompt

            for attempt in range(self.config.max_retries + 1):
                if cancel_check is not None and cancel_check():
                    job.status = JobStatus.CANCELLED
                    job.error = "cancelled"
                    job.touch()
                    break

                if not candidates:
                    # No candidates to render -> nothing more we can do here.
                    break

                self._set_status(job, JobStatus.RUNNING)
                job.retry_count = attempt
                round_records: list[CandidateImageMeta] = []
                round_results: list[QualityResult] = []
                for cand in candidates:
                    self._set_status(job, JobStatus.FETCHING_RESULT)
                    request = PanelExecutionRequest(
                        project_id=project_id,
                        page_id=page_id,
                        panel_id=panel_id,
                        candidate_id=cand.candidate_id,
                        prompt=current_prompt,
                        workflow_id=workflow_id,
                        seed=cand.seed,
                        attempt_index=attempt,
                        width=cand.width,
                        height=cand.height,
                        metadata={"run_id": run_id, "attempt_index": attempt} if run_id else {"attempt_index": attempt},
                    )
                    result = await self._submit(executor, request)
                    if cancel_check is not None and cancel_check():
                        job.status = JobStatus.CANCELLED
                        job.error = "cancelled"
                        job.touch()
                        break
                    image_path = self._save_image(result.image, cand.candidate_id)
                    self._set_status(job, JobStatus.QA_CHECKING)
                    scored = self._score(panel, cand, current_prompt, self.config.threshold)
                    meta = CandidateImageMeta(
                        candidate_id=cand.candidate_id,
                        panel_id=cand.panel_id,
                        seed=cand.seed,
                        workflow_id=workflow_id,
                        image_path=str(image_path),
                        width=cand.width,
                        height=cand.height,
                        score=scored.total(),
                        passed=scored.passed,
                        qa_actions=[a.value for a in scored.suggested_actions],
                    )
                    round_records.append(meta)
                    round_results.append(scored)

                if job.status == JobStatus.CANCELLED:
                    break

                # Drop the previous round's records (keep only the latest attempt).
                job.candidates = round_records

                if not round_records:
                    break

                best = max(round_records, key=lambda c: c.score or 0.0)
                job.select_candidate(best.candidate_id)

                if best.passed:
                    self._set_status(job, JobStatus.COMPLETED)
                    break

                if self.config.strict:
                    # A strict run never guesses on the user's behalf: a quality
                    # rejection waits for a decision instead of burning GPU time
                    # on an automatic retry or substituting a fallback image.
                    self._set_status(job, JobStatus.AWAITING_REVIEW)
                    break

                # Decide whether to retry or fall back.  Pass the failing
                # QA result's issues to the retry controller so it can
                # produce a :class:`QualityIssue` list it understands.
                issues: list[dict[str, object]] = []
                for scored in round_results:
                    if scored.passed:
                        continue
                    for issue in scored.issues:
                        issues.append(dict(issue))
                revised_prompt, decision = self.retry_controller.revise(
                    current_prompt, issues
                )
                if attempt >= self.config.max_retries or decision.action == RetryAction.USE_FALLBACK:
                    job = self._apply_fallback(panel, job, prompt)
                    break
                if decision.action in {RetryAction.REVISE_PROMPT, RetryAction.SIMPLIFY_COMPOSITION}:
                    current_prompt = revised_prompt
                # CHANGE_SEED / CHANGE_WORKFLOW / RETRY_SAME: re-render with the
                # existing prompt but a new base seed; the candidate generator
                # already varies seeds so we simply loop again.
                self._set_status(job, JobStatus.RETRYING)

            # Any non-terminal status means we exited the loop without
            # completing the panel (e.g. zero candidates produced) -- fall
            # back to the safe image so the pipeline never crashes.
            if job.status not in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.AWAITING_REVIEW,
            ):
                if self.config.strict:
                    job.status = JobStatus.FAILED
                    job.error = job.error or "strict run produced no candidate"
                    job.touch()
                else:
                    job = self._apply_fallback(panel, job, prompt)

        except Exception as exc:  # noqa: BLE001
            job.status = JobStatus.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            job.touch()
            log.exception("generation loop failed for %s", job.panel_id)

        job.completed_at = _utc_now_iso()
        write_job(self._jobs_dir() / f"{job.id}.json", job)
        selected_path = self._image_path_for(job)
        return GenerationOutcome(job=job, selected_image_path=selected_path)

    # ------------------------------------------------------------------ utils
    def _apply_fallback(
        self,
        panel: PanelPlan,
        job: GenerationJob,
        prompt: PromptSpec,
    ) -> GenerationJob:
        """Persist a :class:`FallbackGenerator` image and mark the job."""

        fallback = self.fallback.generate(panel, panel_id=job.panel_id)
        image_path = self._save_image(fallback.image, fallback.candidate_id)
        meta = CandidateImageMeta(
            candidate_id=fallback.candidate_id,
            panel_id=fallback.panel_id,
            seed=fallback.seed,
            workflow_id="fallback",
            image_path=str(image_path),
            width=fallback.width,
            height=fallback.height,
            score=0.0,
            passed=False,
            qa_actions=[RetryAction.USE_FALLBACK.value],
        )
        job.candidates.append(meta)
        job.select_candidate(meta.candidate_id)
        job.fallback_used = True
        job.status = JobStatus.COMPLETED
        return job

    def _image_path_for(self, job: GenerationJob) -> Path | None:
        selected = job.selected_candidate()
        if selected is None or selected.image_path is None:
            return None
        return Path(selected.image_path)


# ---------------------------------------------------------- ComfyUI-backed executor
@dataclass
class SequentialPanelResult:
    """What happened to one panel during a sequential run."""

    panel_id: str
    status: str
    outcome: GenerationOutcome | None = None


async def run_panels_sequentially(
    *,
    records: list[PanelRecord],
    loop: GenerationLoop,
    executor: GenerationExecutor,
    prompt_for: Callable[[PanelRecord], PromptSpec],
    workflow_id: str,
    project_id: str,
    persist: Callable[[list[PanelRecord]], Any],
    run_id: str = "",
    cancel_check: Callable[[], bool] | None = None,
) -> list[SequentialPanelResult]:
    """Generate `records` one panel at a time, persisting after each.

    A panel is written to disk before the next one is submitted, so an
    interrupted run resumes from the last completed panel rather than
    re-rendering work that was already paid for. A panel that comes back
    ``AWAITING_REVIEW`` stops the run: the user decides what happens next.

    ``prompt_for`` may raise; the panel is then marked failed and the run stops.
    Nothing is substituted for a prompt that could not be built.
    """
    results: list[SequentialPanelResult] = []

    for record in records:
        if cancel_check is not None and cancel_check():
            log.info("sequential run cancelled before %s", record.panel_id)
            break

        if _panel_is_complete(record):
            results.append(SequentialPanelResult(record.panel_id, "skipped"))
            continue

        try:
            prompt = prompt_for(record)
            if inspect.isawaitable(prompt):
                prompt = await prompt
        except Exception as exc:  # noqa: BLE001 - reported, never faked
            log.warning("prompt build failed for %s: %s", record.panel_id, exc)
            record.status = "failed"
            record.updated_at = datetime.now(timezone.utc)
            persist(records)
            results.append(SequentialPanelResult(record.panel_id, "failed"))
            break

        outcome = await loop.run(
            panel=record.plan,
            page_number=record.page_number,
            prompt=prompt,
            workflow_id=workflow_id,
            executor=executor,
            project_id=project_id,
            cancel_check=cancel_check,
            run_id=run_id,
            panel_id=record.panel_id,
        )

        status = _record_status_for(outcome.job.status)
        if outcome.selected_image_path is not None:
            record.image_path = str(outcome.selected_image_path)
        if status != "cancelled":
            # A cancelled panel was never judged, so its record keeps the status
            # it had; only a real verdict overwrites it.
            record.status = status
        record.workflow_id = workflow_id
        record.history.append(
            {
                "kind": "sequential_generation",
                "job_id": outcome.job.id,
                "at": _utc_now_iso(),
                "job_status": outcome.job.status.value,
            }
        )
        record.updated_at = datetime.now(timezone.utc)
        persist(records)
        results.append(SequentialPanelResult(record.panel_id, status, outcome))

        if status != "generated":
            log.info("sequential run stopped at %s (%s)", record.panel_id, status)
            break

    return results


def _panel_is_complete(record: PanelRecord) -> bool:
    return (
        record.status in {"generated", "approved"}
        and record.image_path is not None
        and Path(record.image_path).exists()
    )


def _record_status_for(job_status: JobStatus) -> str:
    """Map a job outcome onto the panel record status the run should record."""
    if job_status == JobStatus.COMPLETED:
        return "generated"
    if job_status == JobStatus.AWAITING_REVIEW:
        return "rejected"
    if job_status == JobStatus.CANCELLED:
        return "cancelled"
    return "failed"


@dataclass
class ComfyExecutor:
    """A real :class:`GenerationExecutor` backed by :class:`ComfyClient`.

    The loop is satisfied with a single seed per call; this executor
    follows the spec-23.2 pattern of ``submit -> history -> /view`` to
    pull the rendered image bytes.  Failures bubble up as exceptions
    so the loop can mark the job as :attr:`JobStatus.FAILED`.

    The caller is responsible for selecting the workflow (and any
    pre-applied overrides).  When ``api_graph_override`` is supplied it
    is used as-is; otherwise ``workflow_id`` is looked up in
    ``registry``.
    """

    client: Any
    registry: Any
    workflow_id: str = "anime_t2i_default"
    poll_interval_sec: float = 0.05
    poll_timeout_sec: float = 30.0

    async def submit(
        self,
        request: PanelExecutionRequest,
    ) -> GenerationExecutorResult:
        import asyncio
        import io

        from manga_autopilot.services.comfy_client import ComfyClient
        from manga_autopilot.services.workflow_executor import apply_overrides

        wf_id = request.workflow_id or self.workflow_id
        workflow = self.registry.get(wf_id)
        if workflow.api_graph is None:
            raise RuntimeError(
                f"workflow {workflow.workflow_id!r} has no api_graph embedded"
            )
        graph = apply_overrides(
            workflow.api_graph,
            workflow.bindings,
            {
                "positive_prompt": request.prompt.positive,
                "negative_prompt": request.prompt.negative or "",
                "seed": request.seed,
                "width": request.effective_width,
                "height": request.effective_height,
                "steps": request.prompt.steps,
                "cfg": request.prompt.cfg,
            },
        )
        prompt_id = await self.client.submit_workflow(graph)

        # Poll for the history entry.
        deadline = datetime.now(timezone.utc).timestamp() + self.poll_timeout_sec
        entry: dict[str, Any] = {}
        while datetime.now(timezone.utc).timestamp() < deadline:
            history = await self.client.get_history(prompt_id)
            entry = history.get(prompt_id) or {}
            if entry:
                break
            await asyncio.sleep(self.poll_interval_sec)
        if not entry:
            raise RuntimeError(f"timed out waiting for history of {prompt_id}")

        image_refs = ComfyClient.extract_output_images(entry)
        if not image_refs:
            raise RuntimeError(f"no output images for prompt {prompt_id}")
        ref = image_refs[0]
        raw = await self.client.fetch_view(
            ref["filename"],
            subfolder=ref.get("subfolder", ""),
            type=ref.get("type", "output"),
        )
        image = Image.open(io.BytesIO(raw))
        image.load()
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id=prompt_id,
            image=image,
            workflow_id=workflow.workflow_id,
        )


__all__ = [
    "ComfyExecutor",
    "GenerationExecutor",
    "GenerationExecutorResult",
    "GenerationLoop",
    "GenerationLoopConfig",
    "GenerationOutcome",
    "JOBS_SUBDIR",
    "PANELS_SUBDIR",
    "PanelExecutionRequest",
]
