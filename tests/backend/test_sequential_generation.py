"""Strict Anima generation semantics (plan Task 6, steps 3-4).

Generic projects keep the existing candidate/retry behaviour; only a strict run
persists a panel before submitting the next one, refuses to auto-retry a quality
rejection, retries a technical failure exactly once, and reconciles a timed-out
submission against ComfyUI history before resubmitting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from manga_autopilot.models.job import JobStatus
from manga_autopilot.models.page import PanelPlan
from manga_autopilot.models.panel import PanelRecord, write_panel_records
from manga_autopilot.services.generation_job import (
    ExecutorTimeout,
    GenerationExecutorResult,
    GenerationLoop,
    GenerationLoopConfig,
    PanelExecutionRequest,
    run_panels_sequentially,
)
from manga_autopilot.services.prompt_builder import PromptSpec

PASS_THRESHOLD = 0.0
IMPOSSIBLE_THRESHOLD = 1.1


def _plan(panel_number: int) -> PanelPlan:
    return PanelPlan(
        panel_number=panel_number,
        purpose="hero shot",
        shot="medium",
        action="stands tall",
        emotion="determined",
    )


def _record(panel_number: int, *, status: str = "draft", image_path: str | None = None) -> PanelRecord:
    return PanelRecord(
        panel_id=f"panel_{panel_number:03d}",
        page_number=1,
        plan=_plan(panel_number),
        status=status,
        image_path=image_path,
    )


def _prompt() -> PromptSpec:
    return PromptSpec(positive="1girl", negative="", seed=7, width=64, height=64, steps=4, cfg=1.0)


class RecordingExecutor:
    """Fake executor that can fail, time out, and answer reconciliation."""

    def __init__(
        self,
        *,
        raise_times: int = 0,
        timeout_times: int = 0,
        timeout_prompt_id: str = "",
        reconcile_result: bool = False,
        on_submit=None,
    ) -> None:
        self.calls: list[PanelExecutionRequest] = []
        self.reconciled: list[str] = []
        self.events: list[str] = []
        self._raise_times = raise_times
        self._timeout_times = timeout_times
        self._timeout_prompt_id = timeout_prompt_id
        self._reconcile_result = reconcile_result
        self._on_submit = on_submit

    async def submit(self, request: PanelExecutionRequest) -> GenerationExecutorResult:
        self.calls.append(request)
        self.events.append(f"submit:{request.candidate_id}:{request.attempt_index}")
        if self._on_submit is not None:
            self._on_submit(request)
        if self._timeout_times > 0:
            self._timeout_times -= 1
            raise ExecutorTimeout("comfy timed out", prompt_id=self._timeout_prompt_id)
        if self._raise_times > 0:
            self._raise_times -= 1
            raise RuntimeError("comfy refused the prompt")
        return self._result(request.candidate_id, request.workflow_id)

    async def reconcile(self, prompt_id: str) -> GenerationExecutorResult | None:
        self.reconciled.append(prompt_id)
        self.events.append(f"reconcile:{prompt_id}")
        if not self._reconcile_result:
            return None
        return self._result("recovered", "wf")

    @staticmethod
    def _result(candidate_id: str, workflow_id: str) -> GenerationExecutorResult:
        return GenerationExecutorResult(
            candidate_id=candidate_id,
            prompt_id=f"prompt_{candidate_id}",
            image=Image.new("RGB", (64, 64), (10, 20, 30)),
            workflow_id=workflow_id,
        )


def _loop(tmp_path: Path, *, threshold: float, strict: bool, technical_retry_count: int = 1) -> GenerationLoop:
    return GenerationLoop(
        project_root=tmp_path,
        config=GenerationLoopConfig(
            candidate_count=1,
            max_retries=2,
            threshold=threshold,
            strict=strict,
            technical_retry_count=technical_retry_count,
        ),
    )


# ------------------------------------------------------- quality rejection


async def test_strict_quality_rejection_does_not_auto_retry(tmp_path: Path) -> None:
    executor = RecordingExecutor()

    outcome = await _loop(tmp_path, threshold=IMPOSSIBLE_THRESHOLD, strict=True).run(
        panel=_plan(1),
        page_number=1,
        prompt=_prompt(),
        workflow_id="wf",
        executor=executor,
        project_id="p1",
    )

    assert outcome.job.status == JobStatus.AWAITING_REVIEW
    assert len(executor.calls) == 1
    assert outcome.job.retry_count == 0


async def test_generic_quality_rejection_still_retries(tmp_path: Path) -> None:
    executor = RecordingExecutor()

    outcome = await _loop(tmp_path, threshold=IMPOSSIBLE_THRESHOLD, strict=False).run(
        panel=_plan(1),
        page_number=1,
        prompt=_prompt(),
        workflow_id="wf",
        executor=executor,
        project_id="p1",
    )

    assert len(executor.calls) > 1
    assert outcome.job.status != JobStatus.AWAITING_REVIEW


async def test_strict_pass_completes_without_review(tmp_path: Path) -> None:
    executor = RecordingExecutor()

    outcome = await _loop(tmp_path, threshold=PASS_THRESHOLD, strict=True).run(
        panel=_plan(1),
        page_number=1,
        prompt=_prompt(),
        workflow_id="wf",
        executor=executor,
        project_id="p1",
    )

    assert outcome.job.status == JobStatus.COMPLETED
    assert len(executor.calls) == 1


# ------------------------------------------------------ technical failures


async def test_strict_technical_failure_retries_exactly_once(tmp_path: Path) -> None:
    executor = RecordingExecutor(raise_times=1)

    outcome = await _loop(tmp_path, threshold=PASS_THRESHOLD, strict=True).run(
        panel=_plan(1),
        page_number=1,
        prompt=_prompt(),
        workflow_id="wf",
        executor=executor,
        project_id="p1",
    )

    assert len(executor.calls) == 2
    assert outcome.job.status == JobStatus.COMPLETED


async def test_strict_technical_failure_fails_after_its_single_retry(tmp_path: Path) -> None:
    executor = RecordingExecutor(raise_times=5)

    outcome = await _loop(tmp_path, threshold=PASS_THRESHOLD, strict=True).run(
        panel=_plan(1),
        page_number=1,
        prompt=_prompt(),
        workflow_id="wf",
        executor=executor,
        project_id="p1",
    )

    assert len(executor.calls) == 2
    assert outcome.job.status == JobStatus.FAILED
    assert outcome.selected_image_path is None


# ------------------------------------------------------------- timeouts


async def test_timeout_reconciles_history_before_resubmitting(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        timeout_times=1, timeout_prompt_id="prompt-abc", reconcile_result=False
    )

    await _loop(tmp_path, threshold=PASS_THRESHOLD, strict=True).run(
        panel=_plan(1),
        page_number=1,
        prompt=_prompt(),
        workflow_id="wf",
        executor=executor,
        project_id="p1",
    )

    assert executor.reconciled == ["prompt-abc"]
    assert executor.events[:3] == [
        "submit:panel_001_c00:0",
        "reconcile:prompt-abc",
        "submit:panel_001_c00:0",
    ]


async def test_recovered_timeout_is_adopted_without_resubmitting(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        timeout_times=1, timeout_prompt_id="prompt-abc", reconcile_result=True
    )

    outcome = await _loop(tmp_path, threshold=PASS_THRESHOLD, strict=True).run(
        panel=_plan(1),
        page_number=1,
        prompt=_prompt(),
        workflow_id="wf",
        executor=executor,
        project_id="p1",
    )

    assert len(executor.calls) == 1
    assert executor.reconciled == ["prompt-abc"]
    assert outcome.job.status == JobStatus.COMPLETED


async def test_timeout_without_a_prompt_id_is_a_plain_technical_failure(tmp_path: Path) -> None:
    executor = RecordingExecutor(timeout_times=1, timeout_prompt_id="")

    await _loop(tmp_path, threshold=PASS_THRESHOLD, strict=True).run(
        panel=_plan(1),
        page_number=1,
        prompt=_prompt(),
        workflow_id="wf",
        executor=executor,
        project_id="p1",
    )

    assert executor.reconciled == []
    assert len(executor.calls) == 2


# ---------------------------------------------------------- sequential run


async def test_each_panel_is_persisted_before_the_next_one_submits(tmp_path: Path) -> None:
    panels_json = tmp_path / "panels.json"
    records = [_record(1), _record(2), _record(3)]
    write_panel_records(panels_json, records)
    seen: list[list[str]] = []

    def _on_submit(_request: PanelExecutionRequest) -> None:
        stored = json.loads(panels_json.read_text(encoding="utf-8"))
        seen.append([item["status"] for item in stored])

    executor = RecordingExecutor(on_submit=_on_submit)

    await run_panels_sequentially(
        records=records,
        loop=_loop(tmp_path, threshold=PASS_THRESHOLD, strict=True),
        executor=executor,
        prompt_for=lambda record: _prompt(),
        workflow_id="wf",
        project_id="p1",
        persist=lambda current: write_panel_records(panels_json, current),
    )

    assert seen == [
        ["draft", "draft", "draft"],
        ["generated", "draft", "draft"],
        ["generated", "generated", "draft"],
    ]
    final = json.loads(panels_json.read_text(encoding="utf-8"))
    assert [item["status"] for item in final] == ["generated"] * 3


async def test_completed_panels_are_skipped_on_resume(tmp_path: Path) -> None:
    done = tmp_path / "done.png"
    Image.new("RGB", (8, 8)).save(done)
    records = [
        _record(1, status="generated", image_path=str(done)),
        _record(2),
    ]
    executor = RecordingExecutor()

    results = await run_panels_sequentially(
        records=records,
        loop=_loop(tmp_path, threshold=PASS_THRESHOLD, strict=True),
        executor=executor,
        prompt_for=lambda record: _prompt(),
        workflow_id="wf",
        project_id="p1",
        persist=lambda current: None,
    )

    assert [r.status for r in results] == ["skipped", "generated"]
    assert [call.panel_id for call in executor.calls] == ["panel_002"]


async def test_a_rejected_panel_stops_the_sequential_run(tmp_path: Path) -> None:
    records = [_record(1), _record(2)]
    executor = RecordingExecutor()

    results = await run_panels_sequentially(
        records=records,
        loop=_loop(tmp_path, threshold=IMPOSSIBLE_THRESHOLD, strict=True),
        executor=executor,
        prompt_for=lambda record: _prompt(),
        workflow_id="wf",
        project_id="p1",
        persist=lambda current: None,
    )

    assert [r.status for r in results] == ["rejected"]
    assert records[0].status == "rejected"
    assert records[1].status == "draft"
    assert len(executor.calls) == 1


async def test_cancellation_stops_the_run_without_condemning_the_panel(tmp_path: Path) -> None:
    records = [_record(1), _record(2)]
    executor = RecordingExecutor()

    results = await run_panels_sequentially(
        records=records,
        loop=_loop(tmp_path, threshold=PASS_THRESHOLD, strict=True),
        executor=executor,
        prompt_for=lambda record: _prompt(),
        workflow_id="wf",
        project_id="p1",
        persist=lambda current: None,
        cancel_check=lambda: len(executor.calls) >= 1,
    )

    assert [r.status for r in results] == ["cancelled"]
    assert len(executor.calls) == 1
    # A cancelled panel was never judged, so it stays resumable.
    assert records[0].status == "draft"
    assert records[1].status == "draft"


async def test_cancellation_before_the_first_panel_submits_nothing(tmp_path: Path) -> None:
    records = [_record(1)]
    executor = RecordingExecutor()

    results = await run_panels_sequentially(
        records=records,
        loop=_loop(tmp_path, threshold=PASS_THRESHOLD, strict=True),
        executor=executor,
        prompt_for=lambda record: _prompt(),
        workflow_id="wf",
        project_id="p1",
        persist=lambda current: None,
        cancel_check=lambda: True,
    )

    assert results == []
    assert executor.calls == []


async def test_prompt_builder_failure_is_reported_not_faked(tmp_path: Path) -> None:
    records = [_record(1)]

    def _explode(record: PanelRecord) -> PromptSpec:
        raise ValueError("planner produced no segments")

    results = await run_panels_sequentially(
        records=records,
        loop=_loop(tmp_path, threshold=PASS_THRESHOLD, strict=True),
        executor=RecordingExecutor(),
        prompt_for=_explode,
        workflow_id="wf",
        project_id="p1",
        persist=lambda current: None,
    )

    assert [r.status for r in results] == ["failed"]
    assert records[0].status == "failed"
    with pytest.raises(ValueError):
        _explode(records[0])


async def test_panels_from_different_pages_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """The loop's derived panel id is unique only within a page.

    ``run_panels_sequentially`` passes each record's own id, so page 2 panel 1
    no longer writes over page 1 panel 1's candidate image.
    """
    records = [_record(1), _record(1)]
    records[0].panel_id = "p1_01"
    records[1].panel_id = "p2_01"
    records[1].page_number = 2
    executor = RecordingExecutor()

    await run_panels_sequentially(
        records=records,
        loop=_loop(tmp_path, threshold=PASS_THRESHOLD, strict=True),
        executor=executor,
        prompt_for=lambda record: _prompt(),
        workflow_id="wf",
        project_id="p1",
        persist=lambda current: None,
    )

    assert [call.panel_id for call in executor.calls] == ["p1_01", "p2_01"]
    assert [call.candidate_id for call in executor.calls] == ["p1_01_c00", "p2_01_c00"]
    assert records[0].image_path != records[1].image_path
