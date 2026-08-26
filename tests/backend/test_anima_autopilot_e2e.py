"""End-to-end strict Anima run with fake planning and fake rendering.

Nothing here touches an LLM, ComfyUI, or a GPU. The orchestrator, the review
coordinator, the on-disk review board, and the sequential panel runner are all
real; only the hooks that would call out to a model are faked.

Covered: both review pauses actually block, image generation cannot start before
Storyboard approval, panels are persisted one at a time, lettering and export run
only after the final Artwork review, and a restarted run resumes instead of
re-rendering.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.models.page import PanelPlan
from manga_autopilot.models.panel import PanelRecord, write_panel_records
from manga_autopilot.routes import register_all
from manga_autopilot.services.autopilot import (
    AutopilotRun,
    AutopilotState,
    AutopilotStateMachine,
    Orchestrator,
    OrchestratorHooks,
)
from manga_autopilot.services.generation_job import (
    GenerationExecutorResult,
    GenerationLoop,
    GenerationLoopConfig,
    PanelExecutionRequest,
    run_panels_sequentially,
)
from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.review_gate import (
    ARTWORK_FINAL,
    STORY,
    STORYBOARD,
    ReviewCoordinator,
    ReviewPolicy,
    ReviewStore,
)

PROJECT_ID = "proj-anima-e2e"


class FakeComfy:
    """Returns a small image for any request and records what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[PanelExecutionRequest] = []

    async def submit(self, request: PanelExecutionRequest) -> GenerationExecutorResult:
        self.calls.append(request)
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id=f"prompt_{request.candidate_id}",
            image=Image.new("RGB", (64, 64), (200, 180, 160)),
            workflow_id=request.workflow_id,
        )


def _records(pages: int, panels_per_page: int) -> list[PanelRecord]:
    return [
        PanelRecord(
            panel_id=f"p{page}_{panel:02d}",
            page_number=page,
            plan=PanelPlan(panel_number=panel, purpose="beat", shot="medium"),
        )
        for page in range(1, pages + 1)
        for panel in range(1, panels_per_page + 1)
    ]


def _prompt(record: PanelRecord) -> PromptSpec:
    return PromptSpec(
        positive=f"1girl, {record.plan.purpose}",
        negative="",
        seed=1000 + record.plan.panel_number,
        width=64,
        height=64,
        steps=4,
        cfg=1.0,
    )


def _loop(project_root: Path) -> GenerationLoop:
    return GenerationLoop(
        project_root=project_root,
        config=GenerationLoopConfig(
            candidate_count=1, max_retries=1, threshold=0.0, strict=True
        ),
    )


def _hooks(
    project_root: Path,
    executor: FakeComfy,
    order: list[str],
    records: list[PanelRecord],
) -> OrchestratorHooks:
    panels_json = project_root / "panels.json"

    def _mark(name: str):
        async def _hook(run: AutopilotRun) -> dict[str, str]:
            order.append(name)
            return {"step": name}

        return _hook

    async def _generate(run: AutopilotRun) -> list[dict[str, str]]:
        order.append("generate_panels")
        results = await run_panels_sequentially(
            records=records,
            loop=_loop(project_root),
            executor=executor,
            prompt_for=_prompt,
            workflow_id="anima_turbo",
            project_id=PROJECT_ID,
            persist=lambda current: write_panel_records(panels_json, current),
            run_id=run.run_id,
        )
        return [{"panel_id": r.panel_id, "status": r.status} for r in results]

    return OrchestratorHooks(
        validate_input=_mark("validate_input"),
        plan_story=_mark("plan_story"),
        define_characters=_mark("define_characters"),
        generate_character_sheets=_mark("generate_character_sheets"),
        plan_pages=_mark("plan_pages"),
        plan_panels=_mark("plan_panels"),
        build_prompts=_mark("build_prompts"),
        validate_workflow=_mark("validate_workflow"),
        generate_panels=_generate,
        qa_panels=_mark("qa_panels"),
        lettering=_mark("lettering"),
        render_pages=_mark("render_pages"),
        export=_mark("export"),
        finalize=_mark("finalize"),
    )


def _run() -> AutopilotRun:
    return AutopilotRun(
        project_id=PROJECT_ID,
        machine=AutopilotStateMachine(project_id=PROJECT_ID),
        input={"generation_profile_id": "anima_turbo"},
    )


def _coordinator(project_root: Path) -> ReviewCoordinator:
    store = ReviewStore(project_root)
    board = store.load(PROJECT_ID, ReviewPolicy.for_profile("anima_turbo"))
    return ReviewCoordinator(board=board, store=store)


async def _settle() -> None:
    """Let the pipeline task advance until it blocks on a gate."""
    for _ in range(50):
        await asyncio.sleep(0)


# ------------------------------------------------------------------ pauses


async def test_strict_run_pauses_after_story_and_before_generation(tmp_path: Path) -> None:
    executor = FakeComfy()
    order: list[str] = []
    records = _records(2, 2)
    coordinator = _coordinator(tmp_path)
    orchestrator = Orchestrator(
        hooks=_hooks(tmp_path, executor, order, records),
        project_root=tmp_path,
        reviews=coordinator,
    )
    run = _run()

    task = asyncio.create_task(orchestrator.run_pipeline(run))
    await _settle()

    # Blocked on the story gate: planning happened, nothing after it did.
    assert order == ["validate_input", "plan_story"]
    assert run.awaiting_review == STORY

    coordinator.approve(STORY)
    await _settle()

    # Now blocked on the storyboard gate, with no image generated.
    assert "build_prompts" in order
    assert "generate_panels" not in order
    assert executor.calls == []
    assert run.awaiting_review == STORYBOARD

    coordinator.approve(STORYBOARD)
    await _settle()

    assert "generate_panels" in order
    assert run.awaiting_review == ARTWORK_FINAL
    assert "lettering" not in order

    coordinator.approve(ARTWORK_FINAL)
    await asyncio.wait_for(task, timeout=5)

    assert order[-4:] == ["lettering", "render_pages", "export", "finalize"]
    assert run.machine.state == AutopilotState.COMPLETED


async def test_generation_cannot_start_while_storyboard_is_unapproved(tmp_path: Path) -> None:
    executor = FakeComfy()
    order: list[str] = []
    records = _records(1, 2)
    coordinator = _coordinator(tmp_path)
    orchestrator = Orchestrator(
        hooks=_hooks(tmp_path, executor, order, records),
        project_root=tmp_path,
        reviews=coordinator,
    )
    run = _run()

    task = asyncio.create_task(orchestrator.run_pipeline(run))
    await _settle()
    coordinator.approve(STORY)
    await _settle()

    assert executor.calls == []
    assert not (tmp_path / "panels.json").exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_rejected_gate_stops_the_run(tmp_path: Path) -> None:
    executor = FakeComfy()
    order: list[str] = []
    coordinator = _coordinator(tmp_path)
    orchestrator = Orchestrator(
        hooks=_hooks(tmp_path, executor, order, _records(1, 1)),
        project_root=tmp_path,
        reviews=coordinator,
    )
    run = _run()

    task = asyncio.create_task(orchestrator.run_pipeline(run))
    await _settle()
    coordinator.reject(STORY, note="the ending does not follow")

    await asyncio.wait_for(task, timeout=5)

    assert "define_characters" not in order
    assert executor.calls == []
    assert run.machine.state.value.startswith("FAILED")
    assert any(event["kind"] == "review_rejected" for event in run.log)


# ------------------------------------------------------ sequential persistence


async def test_panels_are_persisted_one_at_a_time(tmp_path: Path) -> None:
    executor = FakeComfy()
    order: list[str] = []
    records = _records(2, 2)
    coordinator = _coordinator(tmp_path)
    for gate in (STORY, STORYBOARD, ARTWORK_FINAL):
        coordinator.approve(gate)
    orchestrator = Orchestrator(
        hooks=_hooks(tmp_path, executor, order, records),
        project_root=tmp_path,
        reviews=coordinator,
    )

    await asyncio.wait_for(orchestrator.run_pipeline(_run()), timeout=10)

    stored = json.loads((tmp_path / "panels.json").read_text(encoding="utf-8"))
    assert [item["status"] for item in stored] == ["generated"] * 4
    assert len(executor.calls) == 4
    assert [call.panel_id for call in executor.calls] == ["p1_01", "p1_02", "p2_01", "p2_02"]


async def test_a_restarted_run_resumes_instead_of_re_rendering(tmp_path: Path) -> None:
    records = _records(2, 1)
    coordinator = _coordinator(tmp_path)
    for gate in (STORY, STORYBOARD, ARTWORK_FINAL):
        coordinator.approve(gate)

    first_executor = FakeComfy()
    await asyncio.wait_for(
        Orchestrator(
            hooks=_hooks(tmp_path, first_executor, [], records),
            project_root=tmp_path,
            reviews=coordinator,
        ).run_pipeline(_run()),
        timeout=10,
    )
    assert len(first_executor.calls) == 2

    # Restart with the records as they were persisted.
    from manga_autopilot.models.panel import load_panel_records

    resumed_records = load_panel_records(tmp_path / "panels.json")
    second_executor = FakeComfy()
    resumed_coordinator = _coordinator(tmp_path)

    await asyncio.wait_for(
        Orchestrator(
            hooks=_hooks(tmp_path, second_executor, [], resumed_records),
            project_root=tmp_path,
            reviews=resumed_coordinator,
        ).run_pipeline(_run()),
        timeout=10,
    )

    assert second_executor.calls == [], "already-generated panels must not be re-rendered"
    assert resumed_coordinator.board.is_approved(STORYBOARD) is True


async def test_a_legacy_project_never_pauses(tmp_path: Path) -> None:
    executor = FakeComfy()
    order: list[str] = []
    store = ReviewStore(tmp_path)
    board = store.load("proj-legacy", ReviewPolicy.for_profile(None))
    orchestrator = Orchestrator(
        hooks=_hooks(tmp_path, executor, order, _records(1, 1)),
        project_root=tmp_path,
        reviews=ReviewCoordinator(board=board, store=store),
    )
    run = _run()

    await asyncio.wait_for(orchestrator.run_pipeline(run), timeout=10)

    assert run.machine.state == AutopilotState.COMPLETED
    assert run.awaiting_review == ""
    assert len(executor.calls) == 1


# ---------------------------------------------------------------- HTTP API


@pytest.fixture
async def client(aiohttp_client, tmp_path: Path):
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    return await aiohttp_client(app)


async def _create_anima_project(client) -> str:
    response = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "anima e2e", "id": PROJECT_ID},
    )
    assert response.status in (200, 201)
    patch = await client.patch(
        f"/manga_autopilot/api/projects/{PROJECT_ID}",
        json={"generation_profile_id": "anima_turbo"},
    )
    assert patch.status == 200
    return PROJECT_ID


async def test_reviews_endpoint_reports_the_blocking_gate(client) -> None:
    project_id = await _create_anima_project(client)

    response = await client.get(f"/manga_autopilot/api/projects/{project_id}/reviews")

    assert response.status == 200
    body = await response.json()
    assert body["policy"]["gates"][0] == STORY
    assert body["blocking_gate"] == STORY


async def test_approving_through_the_api_persists_and_advances(client) -> None:
    project_id = await _create_anima_project(client)

    approved = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/reviews/{STORY}/approve",
        json={"note": "reads well", "by": "koudai"},
    )

    assert approved.status == 200
    body = await approved.json()
    assert body["gates"][STORY]["status"] == "approved"
    assert body["blocking_gate"] == STORYBOARD

    again = await client.get(f"/manga_autopilot/api/projects/{project_id}/reviews")
    assert (await again.json())["gates"][STORY]["decisions"][0]["note"] == "reads well"


async def test_rejecting_through_the_api_is_recorded(client) -> None:
    project_id = await _create_anima_project(client)

    response = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/reviews/{STORYBOARD}/reject",
        json={"note": "panel 3 is unreadable"},
    )

    body = await response.json()
    assert body["gates"][STORYBOARD]["status"] == "rejected"
    assert body["gates"][STORYBOARD]["decisions"][0]["note"] == "panel 3 is unreadable"


async def test_an_unknown_gate_is_a_404(client) -> None:
    project_id = await _create_anima_project(client)

    response = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/reviews/colouring/approve"
    )

    assert response.status == 404


async def test_reviews_for_an_unknown_project_is_a_404(client) -> None:
    response = await client.get("/manga_autopilot/api/projects/ghost/reviews")

    assert response.status == 404
