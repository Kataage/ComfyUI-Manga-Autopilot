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
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.review_gate import (
    ARTWORK_EARLY,
    ARTWORK_FINAL,
    REVIEW_GATES,
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


# ------------------------------------------------- the route path, end to end
#
# The tests above drive the Orchestrator directly. These drive the real HTTP
# routes, which is where the coordinator is published, where the early-artwork
# cadence lives, and where an approval has to travel from a REST call back into
# a running pipeline. A fake LLM and a fake executor stand in; no GPU is used.


class _RouteFakeLLM(LLMProvider):
    """Enough JSON to satisfy the strict planners for a 2-page, 1-panel story."""

    def __init__(self) -> None:
        super().__init__(LLMSettings())

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        required = (schema or {}).get("required", [])
        pages = [
            {
                "pageNumber": n,
                "summary": f"Page {n}",
                "emotionalGoal": "determined",
                "visualGoal": "wide shot",
                "panelCount": 1,
            }
            for n in (1, 2)
        ]
        if "title" in required and "pages" in required:
            # Strict mode requires a Story Bible; the planner rejects a plan
            # without one, which is exactly what Task 1 put there.
            return json.dumps(
                {
                    "title": "Route Test",
                    "logline": "A test story.",
                    "genre": "fantasy",
                    "storyBible": {
                        "title": "Route Test",
                        "genre": "fantasy",
                        "tone": "quiet",
                        "theme": "arrival",
                        "world": "a wide field at dusk",
                        "rules": ["the hero never runs"],
                        "timeline": ["dusk"],
                        "locations": {"field": "a wide open field"},
                        "important_objects": {"cloak": "the hero's blue cloak"},
                        "relationships": ["the hero travels alone"],
                        "foreshadowing": ["the far treeline"],
                        "resolved_events": [],
                        "unresolved_events": ["what waits past the trees"],
                    },
                    "pages": pages,
                }
            )
        if "characters" in required:
            return json.dumps(
                {
                    "characters": [
                        {
                            "id": "char_hero",
                            "name": "Hero",
                            "role": "protagonist",
                            "visualTraits": ["blue hair"],
                            "mustKeep": ["blue hair"],
                            "styleHints": "manga",
                        }
                    ]
                }
            )
        if "pages" in required:
            return json.dumps({"pages": pages})
        if "panels" in required:
            return json.dumps(
                {
                    "panels": [
                        {
                            "panelNumber": 1,
                            "purpose": "establishing",
                            "shot": "wide",
                            "cameraAngle": "low",
                            "action": "stands tall",
                            "emotion": "determined",
                            "characters": ["char_hero"],
                            "background": "open field",
                            "visualPriority": "character",
                            "dialogue": [
                                {
                                    "speaker": "Hero",
                                    "text": "go",
                                    "type": "speech",
                                    "characterId": "char_hero",
                                }
                            ],
                        }
                    ]
                }
            )
        if "positive" in required:
            return json.dumps(
                {
                    "positive": "hero standing tall, wide shot, blue hair",
                    "negative": "low quality, blurry",
                    "seed": 12345,
                    "width": 64,
                    "height": 64,
                }
            )
        return "{}"


async def _wait_for_gate(client, project_id, gate, *, timeout=15.0):
    """Poll the review board until `gate` is the blocking one."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        response = await client.get(f"/manga_autopilot/api/projects/{project_id}/reviews")
        last = await response.json()
        if last["blocking_gate"] == gate and last["gates"][gate]["status"] == "awaiting_review":
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"gate {gate} never blocked; board was {last}")


def _panel_statuses(storage_root: Path, project_id: str) -> list[str]:
    path = storage_root / "projects" / project_id / "panels.json"
    if not path.exists():
        return []
    return [item["status"] for item in json.loads(path.read_text(encoding="utf-8"))]


async def _start_strict_run(aiohttp_client, tmp_path: Path, project_id: str):
    executor = FakeComfy()
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    app["manga_llm_provider"] = _RouteFakeLLM()
    app["manga_panel_executor"] = executor
    client = await aiohttp_client(app)

    created = await client.post(
        "/manga_autopilot/api/projects", json={"name": "route e2e", "id": project_id}
    )
    assert created.status in (200, 201)
    patched = await client.patch(
        f"/manga_autopilot/api/projects/{project_id}",
        json={"generation_profile_id": "anima_turbo", "license_acknowledged": True},
    )
    assert patched.status == 200

    started = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "a hero crosses a field",
            "generation_profile_id": "anima_turbo",
            "page_count": 2,
            "threshold": 0.0,
            "candidate_count": 1,
        },
    )
    assert started.status == 202
    return client, executor


async def test_the_route_path_pauses_at_every_gate_and_resumes_over_rest(
    aiohttp_client, tmp_path: Path
) -> None:
    client, executor = await _start_strict_run(aiohttp_client, tmp_path, "proj-route")

    # 1. Story: planning ran, nothing else did.
    await _wait_for_gate(client, "proj-route", STORY)
    assert executor.calls == []

    approved = await client.post(
        "/manga_autopilot/api/projects/proj-route/reviews/story/approve",
        json={"note": "reads fine"},
    )
    assert approved.status == 200

    # 2. Storyboard: still nothing has reached the executor.
    await _wait_for_gate(client, "proj-route", STORYBOARD)
    assert executor.calls == [], "no image may be queued before Storyboard approval"

    await client.post("/manga_autopilot/api/projects/proj-route/reviews/storyboard/approve")

    # 3. Early artwork: page 1 rendered, page 2 has not.
    await _wait_for_gate(client, "proj-route", ARTWORK_EARLY)
    pages_rendered = {call.page_id for call in executor.calls}
    assert pages_rendered == {"page_0001"}, (
        f"only page 1 should render before the early review, got {pages_rendered}"
    )
    assert _panel_statuses(tmp_path, "proj-route")[0] == "generated"

    await client.post("/manga_autopilot/api/projects/proj-route/reviews/artwork_early/approve")

    # 4. Final artwork: the remaining page rendered too.
    await _wait_for_gate(client, "proj-route", ARTWORK_FINAL)
    assert {call.page_id for call in executor.calls} == {"page_0001", "page_0002"}

    await client.post("/manga_autopilot/api/projects/proj-route/reviews/artwork_final/approve")

    # 5. The board clears and nothing blocks any more.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15.0
    board = None
    while loop.time() < deadline:
        response = await client.get("/manga_autopilot/api/projects/proj-route/reviews")
        board = await response.json()
        if board["blocking_gate"] is None:
            break
        await asyncio.sleep(0.05)
    assert board["blocking_gate"] is None
    assert [board["gates"][g]["status"] for g in REVIEW_GATES] == ["approved"] * 4


async def test_a_rejection_over_rest_stops_the_route_run(
    aiohttp_client, tmp_path: Path
) -> None:
    client, executor = await _start_strict_run(aiohttp_client, tmp_path, "proj-reject")

    await _wait_for_gate(client, "proj-reject", STORY)
    rejected = await client.post(
        "/manga_autopilot/api/projects/proj-reject/reviews/story/reject",
        json={"note": "the ending does not follow"},
    )

    assert rejected.status == 200
    body = await rejected.json()
    assert body["gates"][STORY]["status"] == "rejected"

    await asyncio.sleep(0.3)
    assert executor.calls == [], "a rejected story must never reach the executor"
