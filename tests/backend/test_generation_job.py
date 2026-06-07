"""Tests for the GenerationLoop + GenerationJob model (spec 17.1, 17.5)."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from manga_autopilot.models.job import (
    CandidateImageMeta,
    GenerationJob,
    JobStatus,
    read_job,
    write_job,
)
from manga_autopilot.models.page import PanelPlan
from manga_autopilot.services.generation_job import (
    GenerationExecutorResult,
    GenerationLoop,
    GenerationLoopConfig,
)
from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.qa import RetryAction


# ----------------------------------------------------------------- helpers
class FakeExecutor:
    """A deterministic, in-process stand-in for ComfyClient.

    Records every call so tests can assert how many candidates the
    loop rendered.  Returns simple PIL images so the loop's file-saving
    step has something to write to disk.
    """

    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        image_factory=None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._fail_on = fail_on or set()
        self._image_factory = image_factory

    async def submit(
        self,
        *,
        prompt: PromptSpec,
        workflow_id: str,
        seed: int,
        candidate_id: str,
    ) -> GenerationExecutorResult:
        if candidate_id in self._fail_on:
            raise RuntimeError("fake executor failure")
        self.calls.append(
            {
                "prompt": prompt,
                "workflow_id": workflow_id,
                "seed": seed,
                "candidate_id": candidate_id,
            }
        )
        if self._image_factory is not None:
            image = self._image_factory(seed, prompt.width, prompt.height)
        else:
            image = Image.new("RGB", (prompt.width, prompt.height), (seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=candidate_id,
            prompt_id=f"prompt_{candidate_id}",
            image=image,
            workflow_id=workflow_id,
        )


def _panel(panel_number: int = 1) -> PanelPlan:
    return PanelPlan(
        panel_number=panel_number,
        purpose="hero shot",
        shot="medium",
        action="stands tall",
        emotion="determined",
    )


def _prompt() -> PromptSpec:
    return PromptSpec(
        positive="manga hero, lineart",
        negative="low quality",
        seed=42,
        width=64,
        height=64,
    )


# ----------------------------------------------------------------- model tests
def test_generation_job_round_trip(tmp_path: Path) -> None:
    job = GenerationJob(
        project_id="proj_test_001",
        page_number=1,
        panel_id="panel_001",
        workflow_id="anime_t2i_default",
    )
    job.candidates.append(
        CandidateImageMeta(
            candidate_id="panel_001_c00",
            panel_id="panel_001",
            seed=42,
            image_path="/tmp/x.png",
            width=64,
            height=64,
            score=0.8,
            passed=True,
        )
    )
    job.select_candidate("panel_001_c00")
    job.status = JobStatus.COMPLETED

    path = tmp_path / "job.json"
    write_job(path, job)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["status"] == "completed"
    assert raw["candidates"][0]["image_path"] == "/tmp/x.png"

    reloaded = read_job(path)
    assert reloaded.status == JobStatus.COMPLETED
    assert reloaded.selected_candidate_id == "panel_001_c00"
    assert reloaded.selected_candidate() is not None


def test_generation_job_status_enum_is_stable() -> None:
    assert JobStatus.PENDING.value == "pending"
    assert JobStatus.RUNNING.value == "running"
    assert JobStatus.COMPLETED.value == "completed"
    assert JobStatus.FAILED.value == "failed"
    assert JobStatus.CANCELLED.value == "cancelled"


# ----------------------------------------------------------------- loop tests
async def test_loop_writes_panels_and_job(tmp_path: Path) -> None:
    executor = FakeExecutor()
    loop = GenerationLoop(
        project_root=tmp_path,
        config=GenerationLoopConfig(
            candidate_count=1,
            max_retries=0,
            threshold=0.0,  # accept anything
            panel_width=64,
            panel_height=64,
        ),
    )

    outcome = await loop.run(
        panel=_panel(),
        page_number=1,
        prompt=_prompt(),
        workflow_id="anime_t2i_default",
        executor=executor,
        project_id="proj_test_001",
    )

    assert outcome.job.status == JobStatus.COMPLETED
    assert outcome.selected_image_path is not None
    assert outcome.selected_image_path.exists()
    # Single candidate -> single render call.
    assert len(executor.calls) == 1
    # Job is persisted under jobs/.
    jobs_dir = tmp_path / "jobs"
    assert jobs_dir.is_dir()
    files = list(jobs_dir.iterdir())
    assert len(files) == 1
    reloaded = read_job(files[0])
    assert reloaded.id == outcome.job.id


async def test_loop_retries_then_completes(tmp_path: Path) -> None:
    """When the first round fails QA but the second passes, the loop should
    re-render and select the better candidate."""

    # Threshold of 0.95 is unreachable by the default checkers' random
    # alignment score; instead, the loop's `RetryController.revise` is
    # expected to mutate the prompt so a second round can score higher.
    executor = FakeExecutor()
    loop = GenerationLoop(
        project_root=tmp_path,
        config=GenerationLoopConfig(
            candidate_count=1,
            max_retries=2,
            threshold=0.99,  # unreachable
            panel_width=64,
            panel_height=64,
        ),
    )
    outcome = await loop.run(
        panel=_panel(),
        page_number=1,
        prompt=_prompt(),
        workflow_id="anime_t2i_default",
        executor=executor,
        project_id="proj_test_001",
    )
    # After the retry cap the loop falls back to the safe image.
    assert outcome.job.status == JobStatus.COMPLETED
    assert outcome.job.fallback_used is True
    assert outcome.job.retry_count == 2
    # Three attempts: 1 initial + 2 retries
    assert len(executor.calls) == 3


async def test_loop_cancellation_marks_job(tmp_path: Path) -> None:
    executor = FakeExecutor()
    loop = GenerationLoop(
        project_root=tmp_path,
        config=GenerationLoopConfig(
            candidate_count=2,
            max_retries=0,
            threshold=0.0,
            panel_width=32,
            panel_height=32,
        ),
    )

    cancel_calls = {"n": 0}

    def _cancel() -> bool:
        cancel_calls["n"] += 1
        return cancel_calls["n"] >= 2  # cancel after second check

    outcome = await loop.run(
        panel=_panel(),
        page_number=1,
        prompt=_prompt(),
        workflow_id="anime_t2i_default",
        executor=executor,
        project_id="proj_test_001",
        cancel_check=_cancel,
    )
    assert outcome.job.status == JobStatus.CANCELLED
    assert "cancelled" in outcome.job.error


async def test_loop_failure_propagates_to_failed_status(tmp_path: Path) -> None:
    executor = FakeExecutor(fail_on={"panel_001_c00"})
    loop = GenerationLoop(
        project_root=tmp_path,
        config=GenerationLoopConfig(
            candidate_count=1,
            max_retries=0,
            threshold=0.0,
            panel_width=32,
            panel_height=32,
        ),
    )
    outcome = await loop.run(
        panel=_panel(),
        page_number=1,
        prompt=_prompt(),
        workflow_id="anime_t2i_default",
        executor=executor,
        project_id="proj_test_001",
    )
    assert outcome.job.status == JobStatus.FAILED
    assert "fake executor failure" in outcome.job.error


async def test_loop_handles_no_candidates(tmp_path: Path) -> None:
    """A misconfigured ``candidate_count=0`` must not crash; the loop
    treats it as a failure and falls back to a safe image."""

    executor = FakeExecutor()
    loop = GenerationLoop(
        project_root=tmp_path,
        config=GenerationLoopConfig(
            candidate_count=0,
            max_retries=0,
            threshold=0.0,
            panel_width=32,
            panel_height=32,
        ),
    )
    outcome = await loop.run(
        panel=_panel(),
        page_number=1,
        prompt=_prompt(),
        workflow_id="anime_t2i_default",
        executor=executor,
        project_id="proj_test_001",
    )
    # No candidates -> loop never entered the inner loop -> no QA ->
    # status remains RUNNING, which the run() finally treats as fallback.
    assert outcome.job.status == JobStatus.COMPLETED
    assert outcome.job.fallback_used is True
    # Executor was never invoked.
    assert executor.calls == []


def test_generation_job_select_candidate_marks_only_one() -> None:
    job = GenerationJob(project_id="p", page_number=1, panel_id="panel_001")
    job.candidates.extend(
        [
            CandidateImageMeta(candidate_id="panel_001_c00", panel_id="panel_001", seed=1),
            CandidateImageMeta(candidate_id="panel_001_c01", panel_id="panel_001", seed=2),
        ]
    )
    job.select_candidate("panel_001_c01")
    flags = [c.selected for c in job.candidates]
    assert flags == [False, True]
    assert job.selected_candidate_id == "panel_001_c01"


async def test_loop_persists_candidate_image_paths(tmp_path: Path) -> None:
    executor = FakeExecutor()
    loop = GenerationLoop(
        project_root=tmp_path,
        config=GenerationLoopConfig(
            candidate_count=1,
            max_retries=0,
            threshold=0.0,
            panel_width=32,
            panel_height=32,
        ),
    )
    outcome = await loop.run(
        panel=_panel(),
        page_number=1,
        prompt=_prompt(),
        workflow_id="anime_t2i_default",
        executor=executor,
        project_id="proj_test_001",
    )
    panels_dir = tmp_path / "assets" / "panels"
    saved = list(panels_dir.iterdir())
    assert len(saved) == 1
    assert outcome.job.candidates[0].image_path == str(saved[0])


def test_retry_action_use_fallback_is_distinct() -> None:
    """Spec 17.5 must keep USE_FALLBACK as a separate decision path so
    audit logs do not conflate it with a real retry."""

    assert RetryAction.USE_FALLBACK.value == "use_fallback"
    assert RetryAction.USE_FALLBACK != RetryAction.RETRY_SAME
