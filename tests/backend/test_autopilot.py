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
    assert run.machine.state == AutopilotState.PANELS_GENERATING
    ctrl.cancel("p1")
    assert run.machine.state == AutopilotState.CANCELLED


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
