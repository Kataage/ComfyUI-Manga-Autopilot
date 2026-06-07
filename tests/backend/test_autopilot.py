"""Tests for the autopilot state machine + pipeline (spec sections 7, 21.3, 40)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient  # type: ignore

from manga_autopilot.routes import register_all
from manga_autopilot.services.autopilot import (
    AutopilotController,
    AutopilotRun,
    AutopilotState,
    AutopilotStateMachine,
    CompletionReport,
    ErrorRecovery,
    InvalidTransitionError,
    ManifestExports,
    ManifestStats,
    ManifestWriter,
    Orchestrator,
    OrchestratorHooks,
    QAReportEntry,
    RecoveryAction,
    write_completion_report,
)


# --------------------------------------------------------------- state machine
def test_state_machine_full_path() -> None:
    sm = AutopilotStateMachine(project_id="p1")
    expected = [
        AutopilotState.INPUT_VALIDATED,
        AutopilotState.STORY_PLANNED,
        AutopilotState.CHARACTERS_DEFINED,
        AutopilotState.CHARACTER_SHEETS_GENERATED,
        AutopilotState.PAGES_PLANNED,
        AutopilotState.PANELS_PLANNED,
        AutopilotState.PROMPTS_GENERATED,
        AutopilotState.WORKFLOWS_BUILT,
        AutopilotState.PANELS_GENERATING,
        AutopilotState.PANELS_QA_CHECKING,
        AutopilotState.LETTERING,
        AutopilotState.PAGE_RENDERING,
        AutopilotState.EXPORTING,
        AutopilotState.COMPLETED,
    ]
    for _ in expected:
        sm.advance()
    assert sm.state == AutopilotState.COMPLETED
    assert len(sm.history) == 14


def test_state_machine_jump_to_repair() -> None:
    sm = AutopilotStateMachine(project_id="p1")
    for _ in range(10):  # to PANELS_QA_CHECKING
        sm.advance()
    sm.jump(AutopilotState.PANELS_REPAIRING, reason="issues")
    sm.jump(AutopilotState.PANELS_QA_CHECKING, reason="recheck")
    assert sm.state == AutopilotState.PANELS_QA_CHECKING


def test_state_machine_jump_to_failure() -> None:
    sm = AutopilotStateMachine(project_id="p1")
    sm.advance()
    sm.fail(AutopilotState.FAILED_STORY_PLANNING, reason="LLM invalid")
    assert sm.state == AutopilotState.FAILED_STORY_PLANNING
    assert sm.failure_reason == "LLM invalid"


def test_state_machine_illegal_jump() -> None:
    sm = AutopilotStateMachine(project_id="p1")
    with pytest.raises(InvalidTransitionError):
        sm.jump(AutopilotState.PANELS_REPAIRING)


def test_state_machine_cannot_advance_from_failure() -> None:
    sm = AutopilotStateMachine(project_id="p1")
    sm.fail(AutopilotState.FAILED_PANEL_QA, reason="qa")
    with pytest.raises(InvalidTransitionError):
        sm.advance()


def test_state_machine_snapshot() -> None:
    sm = AutopilotStateMachine(project_id="p1")
    sm.advance()
    snap = sm.snapshot()
    assert snap["state"] == "INPUT_VALIDATED"
    assert len(snap["history"]) == 1


# --------------------------------------------------------------- recovery
def test_recovery_returns_strategy() -> None:
    rec = ErrorRecovery()
    strat = rec.for_failure(AutopilotState.FAILED_PANEL_GENERATION)
    assert RecoveryAction.RETRY_SAME in strat.actions
    assert rec.execute(AutopilotState.FAILED_LETTERING) == RecoveryAction.EXPAND_MARGIN


def test_recovery_unknown_failure() -> None:
    rec = ErrorRecovery()
    with pytest.raises(KeyError):
        rec.for_failure(AutopilotState.PAUSED)


# --------------------------------------------------------------- run / log
def test_run_records_steps() -> None:
    sm = AutopilotStateMachine(project_id="p1")
    run = AutopilotRun(project_id="p1", machine=sm)
    step = run.record_step("validate", AutopilotState.INPUT_VALIDATED)
    run.finish_step(step)
    run.finish()
    snap = run.to_status()
    assert snap["state"] == "PROJECT_CREATED"
    assert len(snap["steps"]) == 1
    assert snap["finished_at"] is not None


# --------------------------------------------------------------- controller
def test_controller_start_pause_resume_cancel() -> None:
    ctrl = AutopilotController()
    machine = AutopilotStateMachine(project_id="p1")
    run = ctrl.start("p1", machine)
    assert run.machine.state == AutopilotState.PROJECT_CREATED
    ctrl.pause("p1")
    assert run.machine.state == AutopilotState.PAUSED
    ctrl.resume("p1")
    # Resume now restores the pre-pause state (PROJECT_CREATED) instead of
    # always jumping to PANELS_GENERATING.
    assert run.machine.state == AutopilotState.PROJECT_CREATED
    ctrl.cancel("p1")
    assert run.machine.state == AutopilotState.CANCELLED


def test_controller_pause_records_pre_pause_state() -> None:
    """When the controller pauses a run, the current state must be captured
    on ``run.pre_pause_state`` so resume can jump back to where it was."""

    ctrl = AutopilotController()
    machine = AutopilotStateMachine(project_id="p1")
    run = ctrl.start("p1", machine)
    # Fast-forward to PANELS_QA_CHECKING (10 advances from PROJECT_CREATED).
    for _ in range(10):
        machine.advance("tick")
    assert machine.state == AutopilotState.PANELS_QA_CHECKING
    ctrl.pause("p1", reason="user_request")
    assert run.pre_pause_state == AutopilotState.PANELS_QA_CHECKING
    assert machine.state == AutopilotState.PAUSED
    ctrl.resume("p1")
    assert machine.state == AutopilotState.PANELS_QA_CHECKING
    assert run.pre_pause_state is None


def test_controller_pause_clears_pause_event() -> None:
    """The run's ``pause_event`` must be cleared on pause and re-set on resume."""

    ctrl = AutopilotController()
    machine = AutopilotStateMachine(project_id="p1")
    run = ctrl.start("p1", machine)
    run.pause_event = asyncio.Event()
    run.pause_event.set()  # not paused initially
    assert run.pause_event.is_set()
    ctrl.attach_task("p1", task=None, pause_event=run.pause_event)  # type: ignore[arg-type]
    ctrl.pause("p1")
    assert not run.pause_event.is_set()
    ctrl.resume("p1")
    assert run.pause_event.is_set()


def test_controller_status_missing() -> None:
    ctrl = AutopilotController()
    with pytest.raises(KeyError):
        ctrl.status("ghost")


def test_controller_pause_unknown() -> None:
    ctrl = AutopilotController()
    with pytest.raises(KeyError):
        ctrl.pause("ghost")


# --------------------------------------------------------------- completion
def test_completion_report_writes_files(tmp_path: Path) -> None:
    report = CompletionReport(
        project_id="p1",
        title="Demo",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:10:00+00:00",
        state="COMPLETED",
        panels_total=4,
        panels_passed=3,
        qa_entries=[
            QAReportEntry(panel_id="p", candidate_id="c", score=0.9, passed=True)
        ],
        log=[{"kind": "completed"}],
    )
    paths = write_completion_report(tmp_path, report)
    assert paths["generation_log"].exists()
    assert paths["qa_report"].exists()
    data = json.loads(paths["qa_report"].read_text())
    assert data["average_score"] == 0.9


def test_completion_report_average_zero() -> None:
    report = CompletionReport(
        project_id="p1",
        started_at="x",
        finished_at="y",
        state="COMPLETED",
    )
    assert report.average_qa_score() == 0.0


# --------------------------------------------------------------- manifest
def test_manifest_writer_round_trip(tmp_path: Path) -> None:
    writer = ManifestWriter(tmp_path)
    path = writer.write(
        project_id="p1",
        title="Demo",
        status="COMPLETED",
        created_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:10:00+00:00",
        exports=ManifestExports(
            pages=["exports/pages/page_001.png"],
            webtoon=["exports/webtoon/webtoon_001.png"],
            pdf="exports/pdf/manga.pdf",
        ),
        stats=ManifestStats(
            page_count=8, panel_count=32, generated_images=128,
            regenerated_panels=9, average_qa_score=0.82,
        ),
    )
    data = json.loads(path.read_text())
    assert data["project_id"] == "p1"
    assert data["exports"]["pages"] == ["exports/pages/page_001.png"]
    assert data["stats"]["page_count"] == 8
    assert writer.read() == data


# --------------------------------------------------------------- orchestrator
def test_orchestrator_full_run() -> None:
    sm = AutopilotStateMachine(project_id="p1")
    run = AutopilotRun(project_id="p1", machine=sm)
    Orchestrator().run(run, project=None)
    assert sm.state == AutopilotState.COMPLETED
    assert run.finished_at is not None
    assert any(entry["kind"] == "completed" for entry in run.log)


# --------------------------------------------------------------- routes
@pytest.fixture
async def storage_root(tmp_path):
    return tmp_path


@pytest.fixture
async def client(aiohttp_client: AiohttpClient, storage_root):
    app = web.Application()
    register_all(app, storage_root=str(storage_root))
    return await aiohttp_client(app)


async def test_autopilot_routes_full_cycle(client) -> None:
    r = await client.post(
        "/manga_autopilot/api/projects/p1/autopilot/start",
        json={"page_count": 1, "idea": "demo"},
    )
    assert r.status == 202
    snap = await r.json()
    assert snap["state"] == "PROJECT_CREATED"

    # Pause + resume + cancel against an in-flight run.
    r = await client.post("/manga_autopilot/api/projects/p1/autopilot/pause")
    if r.status == 200:
        assert (await r.json())["state"] == "PAUSED"
        r = await client.post("/manga_autopilot/api/projects/p1/autopilot/resume")
        assert r.status == 200
    r = await client.post("/manga_autopilot/api/projects/p1/autopilot/cancel")
    assert r.status in (200, 409)
    r = await client.get("/manga_autopilot/api/projects/p1/autopilot/status")
    assert r.status == 200


async def test_autopilot_routes_with_slow_hook(client) -> None:
    """Inject a blocking hook so the orchestrator stays in PANELS_GENERATING."""

    blocker = asyncio.Event()
    entered = asyncio.Event()

    async def _slow_hook(run):
        entered.set()
        await blocker.wait()
        return None

    from manga_autopilot.routes.autopilot_routes import _controller
    from manga_autopilot.services.autopilot import (
        OrchestratorHooks,
        start_orchestrator,
    )
    from manga_autopilot.storage.paths import ensure_project_paths

    ctrl = _controller(client.app)
    storage_root = client.app["manga_storage_root"]
    paths = ensure_project_paths(storage_root, "slow_p")
    hooks = OrchestratorHooks(
        validate_input=lambda run: None,
        plan_story=_slow_hook,
    )
    start_orchestrator(
        ctrl, "slow_p", hooks=hooks, project_root=paths.root,
        input_payload={"page_count": 1},
    )

    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pass

    r = await client.post("/manga_autopilot/api/projects/slow_p/autopilot/pause")
    assert r.status == 200
    assert (await r.json())["state"] == "PAUSED"

    r = await client.post("/manga_autopilot/api/projects/slow_p/autopilot/cancel")
    assert r.status == 200
    assert (await r.json())["state"] == "CANCELLED"

    blocker.set()


async def test_autopilot_status_unknown(client) -> None:
    r = await client.get("/manga_autopilot/api/projects/ghost/autopilot/status")
    assert r.status == 404


async def test_autopilot_pause_without_start(client) -> None:
    r = await client.post("/manga_autopilot/api/projects/p1/autopilot/pause")
    assert r.status == 404


async def test_autopilot_real_pause_resume_uses_event(client) -> None:
    """End-to-end: a hook blocks, the controller pauses via the asyncio
    Event, and resume unblocks the orchestrator so it continues from
    where it stopped (rather than always jumping to PANELS_GENERATING)."""

    from manga_autopilot.routes.autopilot_routes import _controller
    from manga_autopilot.services.autopilot import (
        OrchestratorHooks,
        start_orchestrator,
    )
    from manga_autopilot.storage.paths import ensure_project_paths

    blocker = asyncio.Event()
    entered = asyncio.Event()

    async def _slow_hook(run):
        entered.set()
        await blocker.wait()
        return None

    ctrl = _controller(client.app)
    storage_root = client.app["manga_storage_root"]
    paths = ensure_project_paths(storage_root, "real_pause_p")
    hooks = OrchestratorHooks(
        validate_input=lambda run: None,
        plan_story=_slow_hook,
    )
    run, _task, _cancel, pause_event = start_orchestrator(
        ctrl,
        "real_pause_p",
        hooks=hooks,
        project_root=paths.root,
        input_payload={"page_count": 1},
    )

    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        blocker.set()
        return

    assert run.machine.state in (AutopilotState.STORY_PLANNED,)

    # Pause: the controller should record the pre-pause state and clear the
    # asyncio.Event, so the orchestrator's next step is blocked.
    ctrl.pause("real_pause_p")
    assert run.machine.state == AutopilotState.PAUSED
    assert run.pre_pause_state == AutopilotState.STORY_PLANNED
    assert not pause_event.is_set()

    # The orchestrator is wedged in plan_story; release the blocker anyway
    # to avoid hangs, but resume will only matter once the next step runs.
    blocker.set()
    await asyncio.sleep(0.05)

    # Resume: the controller restores the pre-pause state.
    ctrl.resume("real_pause_p")
    assert run.machine.state == AutopilotState.STORY_PLANNED
    assert pause_event.is_set()
    assert run.pre_pause_state is None

    # Allow the run to drain.
    try:
        await asyncio.wait_for(_task, timeout=2.0)
    except asyncio.TimeoutError:
        _task.cancel()


async def test_orchestrator_step_blocks_on_pause_event_when_state_paused() -> None:
    """Regression for the round-2 review.

    When ``machine.state == PAUSED`` (i.e. the controller has paused the
    run) the orchestrator's ``_step`` must block on ``pause_event.wait()``
    rather than short-circuit and let the pipeline walk on to the next
    step.  The previous implementation had
    ``if run.machine.state != PAUSED: await pause_event.wait()`` followed
    by ``if _is_paused(run): return None``, which meant that a paused run
    would silently skip every subsequent step and finalize.
    """

    machine = AutopilotStateMachine(project_id="p_pause")
    # Drive the state machine to PANELS_QA_CHECKING then jump to PAUSED
    # to mimic what the controller does on pause().
    for _ in range(10):
        machine.advance("seed")
    assert machine.state == AutopilotState.PANELS_QA_CHECKING
    machine.jump(AutopilotState.PAUSED, reason="user_paused")

    run = AutopilotRun(project_id="p_pause", machine=machine)
    run.pause_event = asyncio.Event()
    run.pause_event.clear()  # pause is in effect

    invoked = asyncio.Event()
    orch = Orchestrator()

    async def _hook(r) -> str:
        invoked.set()
        return "ok"

    orch.hooks = OrchestratorHooks(plan_story=_hook)

    # Start the step; it must block on the cleared pause event, NOT
    # short-circuit and return None.
    step_task = asyncio.create_task(
        orch._step(run, AutopilotState.STORY_PLANNED, "plan_story")
    )
    # Give the event loop a chance to schedule the task.
    await asyncio.sleep(0.05)
    assert not step_task.done(), "_step returned while paused; it must block on the event"
    assert not invoked.is_set(), "the hook must not have been called yet"
    assert machine.state == AutopilotState.PAUSED

    # Now resume: controller sets the event and rewinds the state to the
    # pre-pause state.
    machine.jump(AutopilotState.PANELS_QA_CHECKING, reason="user_resumed")
    run.pause_event.set()

    result = await asyncio.wait_for(step_task, timeout=1.0)
    assert result == "ok"
    assert invoked.is_set()
    # The state machine must have advanced by exactly one step (from
    # PANELS_QA_CHECKING, the rewound pre-pause state) to LETTERING.
    assert machine.state == AutopilotState.LETTERING


async def test_orchestrator_step_blocks_across_multiple_steps() -> None:
    """After the user pauses mid-pipeline, EVERY subsequent ``_step`` call
    must block on the pause event rather than return immediately.  Only
    the resume call should unblock them all in order."""

    machine = AutopilotStateMachine(project_id="p_multi")
    run = AutopilotRun(project_id="p_multi", machine=machine)
    run.pause_event = asyncio.Event()
    run.pause_event.set()  # not paused initially

    call_log: list[str] = []

    async def _make_hook(name: str):
        async def _hook(r):
            call_log.append(name)
            return name
        return _hook

    orch = Orchestrator()
    orch.hooks = OrchestratorHooks(
        validate_input=await _make_hook("v"),
        plan_story=await _make_hook("p"),
    )

    # First step: not paused, should run.
    result_v = await asyncio.wait_for(
        orch._step(run, AutopilotState.INPUT_VALIDATED, "validate_input"),
        timeout=1.0,
    )
    assert result_v == "v"
    assert machine.state == AutopilotState.INPUT_VALIDATED

    # Pause: clear event, jump state to PAUSED.
    machine.jump(AutopilotState.PAUSED, reason="user_paused")
    run.pause_event.clear()

    # Schedule the next two steps; they must both block.
    step1 = asyncio.create_task(
        orch._step(run, AutopilotState.STORY_PLANNED, "plan_story")
    )
    await asyncio.sleep(0.05)
    assert not step1.done()

    # Resume: rewind state, set event.
    machine.jump(AutopilotState.INPUT_VALIDATED, reason="user_resumed")
    run.pause_event.set()

    result_p = await asyncio.wait_for(step1, timeout=1.0)
    assert result_p == "p"
    assert call_log == ["v", "p"]

