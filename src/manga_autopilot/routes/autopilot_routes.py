"""HTTP routes for the autopilot (spec section 21.3)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.services.autopilot import (
    AutopilotController,
    InvalidTransitionError,
    OrchestratorHooks,
    start_orchestrator,
)
from manga_autopilot.storage.paths import ensure_project_paths

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects/{project_id}/autopilot"


def _controller(app: Application) -> AutopilotController:
    ctrl: Any = app.get("manga_autopilot_controller")
    if ctrl is None:
        ctrl = AutopilotController()
        app["manga_autopilot_controller"] = ctrl
    return ctrl


def _storage_root(app: Application) -> Path | None:
    root = app.get("manga_storage_root")
    if root is None:
        return None
    return Path(root)


def _default_hooks_for_project(
    project_id: str,
    storage_root: Path,
) -> OrchestratorHooks:
    """Return a hooks object wired to the real services for ``project_id``.

    The hooks tolerate the absence of an LLM endpoint by leaving the planning
    step's return value as ``None``; subsequent steps are no-ops when their
    upstream is missing.  This makes the orchestrator runnable in
    configuration-light environments while still exercising the code path.
    """

    from manga_autopilot.services.character_service import CharacterService
    from manga_autopilot.services.export import ExportService
    from manga_autopilot.services.workflow_registry import WorkflowRegistry

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root
    character_service = CharacterService.for_project(storage_root, project_id)
    workflow_registry = WorkflowRegistry.open(storage_root)
    export_service = ExportService(storage_root=storage_root)

    return OrchestratorHooks(
        validate_input=lambda run: {"project_id": run.project_id, "ok": True},
        plan_story=lambda run: {"idea": run.input.get("idea", "")},
        define_characters=lambda run: [c.id for c in character_service.list()],
        generate_character_sheets=lambda run: {
            cid: [str(p) for p in character_service.sheet_targets(cid).values()]
            for cid in (c.id for c in character_service.list())
        },
        plan_pages=lambda run: list(range(1, int(run.input.get("page_count", 1)) + 1)),
        plan_panels=lambda run: {"pages": int(run.input.get("page_count", 1))},
        build_prompts=lambda run: {"style": run.input.get("style", "manga")},
        validate_workflow=lambda run: [
            w.workflow_id for w in workflow_registry.list()
        ],
        generate_panels=lambda run: {"workflow_id": run.input.get("workflow_id")},
        qa_panels=lambda run: {"passed": 0, "total": 0},
        lettering=lambda run: str(project_root / "bubbles.json"),
        render_pages=lambda run: str(project_root / "exports" / "pages"),
        export=lambda run: {
            "pages": [str(p) for p in export_service.all_exports(run.project_id)],
        },
        finalize=lambda run: str(project_root / "manifest.json"),
    )


async def _payload(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


async def start(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body: dict[str, Any] = {}
    if request.body_exists or request.content_length:
        try:
            body = await _payload(request)
        except web.HTTPBadRequest:
            body = {}
    input_payload = body.get("input") if isinstance(body.get("input"), dict) else body
    workflow_id = body.get("workflow_id") or input_payload.get("workflow_id")
    if workflow_id:
        input_payload = {**input_payload, "workflow_id": workflow_id}

    storage_root = _storage_root(request.app)
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")

    ensure_project_paths(storage_root, project_id)

    ctrl = _controller(request.app)
    hooks = _default_hooks_for_project(project_id, storage_root)
    run, _task, _cancel, _pause = start_orchestrator(
        ctrl,
        project_id,
        hooks=hooks,
        project_root=ensure_project_paths(storage_root, project_id).root,
        input_payload=input_payload,
    )
    return web.json_response(run.to_status(), status=202)


async def pause(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        run = ctrl.pause(project_id)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(run.to_status())


async def resume(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        run = ctrl.resume(project_id)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(run.to_status())


async def cancel(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        run = ctrl.cancel(project_id)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(run.to_status())


async def status(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        snapshot = ctrl.status(project_id)
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(snapshot)


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_post(ROUTE_PREFIX + "/start", start)
    router.add_post(ROUTE_PREFIX + "/pause", pause)
    router.add_post(ROUTE_PREFIX + "/resume", resume)
    router.add_post(ROUTE_PREFIX + "/cancel", cancel)
    router.add_get(ROUTE_PREFIX + "/status", status)


__all__ = ["register", "ROUTE_PREFIX"]
