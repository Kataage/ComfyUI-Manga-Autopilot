"""HTTP routes for the workflow registry (spec section 21.5)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.models.workflow import (
    WorkflowValidationError,
    validate_api_graph,
)
from manga_autopilot.services.workflow_registry import (
    WorkflowAlreadyExistsError,
    WorkflowNotFoundError,
    WorkflowRegistry,
)

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/workflows"


def _serialise(workflow_id: str, wf) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "name": wf.name,
        "type": wf.type_value(),
        "file": wf.file,
        "bindings": {k: {"node_id": v.node_id, "input": v.input} for k, v in wf.bindings.items()},
        "description": wf.description,
    }


def _registry(app: Application) -> WorkflowRegistry:
    reg = app.get("manga_workflow_registry")
    if not isinstance(reg, WorkflowRegistry):
        raise web.HTTPInternalServerError(text="workflow registry is not initialised")
    return reg


def _payload_or_400(request: web.Request) -> dict[str, Any]:
    try:
        body = request.json()  # type: ignore[func-returns-value]
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


async def _payload_or_400_async(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


# ----------------------------------------------------------------- GET list
async def list_workflows(request: web.Request) -> web.Response:
    reg = _registry(request.app)
    return web.json_response([_serialise(w.workflow_id, w) for w in reg.list()])


# ----------------------------------------------------------------- POST reg
async def create_workflow(request: web.Request) -> web.Response:
    body = await _payload_or_400_async(request)
    reg = _registry(request.app)
    try:
        wf = reg.register(body)
    except WorkflowValidationError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    except WorkflowAlreadyExistsError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    return web.json_response(_serialise(wf.workflow_id, wf), status=201)


# ----------------------------------------------------------------- GET one
async def get_workflow(request: web.Request) -> web.Response:
    wid = request.match_info["workflow_id"]
    reg = _registry(request.app)
    try:
        wf = reg.get(wid)
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(_serialise(wid, wf))


# ----------------------------------------------------------------- PATCH
async def update_workflow(request: web.Request) -> web.Response:
    wid = request.match_info["workflow_id"]
    body = await _payload_or_400_async(request)
    reg = _registry(request.app)
    try:
        wf = reg.update(wid, body)
    except WorkflowValidationError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(_serialise(wid, wf))


# ----------------------------------------------------------------- DELETE
async def delete_workflow(request: web.Request) -> web.Response:
    wid = request.match_info["workflow_id"]
    reg = _registry(request.app)
    try:
        reg.delete(wid)
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.Response(status=204)


# ------------------------------------------------------ POST /validate
async def validate_workflow(request: web.Request) -> web.Response:
    wid = request.match_info["workflow_id"]
    reg = _registry(request.app)
    try:
        wf = reg.get(wid)
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc

    graph_raw = wf.api_graph
    if graph_raw is None:
        # Re-read the raw payload to include api_graph.
        raw_path = reg._workflow_path(wid)  # type: ignore[attr-defined]
        if raw_path.exists():
            import json as _json

            try:
                raw_payload = _json.loads(raw_path.read_text("utf-8"))
                graph_raw = raw_payload.get("api_graph")
            except _json.JSONDecodeError:
                graph_raw = None
    try:
        cleaned = validate_api_graph(graph_raw) if graph_raw is not None else {}
    except WorkflowValidationError as exc:
        return web.json_response(
            {"ok": False, "errors": [str(exc)]},
            status=200,
        )
    return web.json_response(
        {
            "ok": True,
            "workflow_id": wid,
            "nodes": list(cleaned),
            "required_bindings": list(wf.required_bindings()),
            "missing_bindings": sorted(set(wf.required_bindings()) - set(wf.bindings)),
        }
    )


# ------------------------------------------------------ POST /test-run
async def test_run_workflow(request: web.Request) -> web.Response:
    """Stub that records the test-run request. ComfyUI dispatch is in #43."""

    wid = request.match_info["workflow_id"]
    reg = _registry(request.app)
    try:
        reg.get(wid)
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(
        {
            "ok": True,
            "workflow_id": wid,
            "prompt_id": None,
            "message": "test-run is not yet wired to ComfyUI (see #43)",
        }
    )


def register(router) -> None:  # type: ignore[no-untyped-def]
    """Register workflow routes on a ComfyUI router or :class:`Application`."""

    target = router.router if hasattr(router, "router") else router
    target.add_get(ROUTE_PREFIX, list_workflows)
    target.add_post(ROUTE_PREFIX, create_workflow)
    target.add_get(f"{ROUTE_PREFIX}/{{workflow_id}}", get_workflow)
    target.add_patch(f"{ROUTE_PREFIX}/{{workflow_id}}", update_workflow)
    target.add_delete(f"{ROUTE_PREFIX}/{{workflow_id}}", delete_workflow)
    target.add_post(f"{ROUTE_PREFIX}/{{workflow_id}}/validate", validate_workflow)
    target.add_post(f"{ROUTE_PREFIX}/{{workflow_id}}/test-run", test_run_workflow)
    log.debug("registered workflow routes under %s", ROUTE_PREFIX)


def register_routes(app: Application, *, prefix: str = ROUTE_PREFIX) -> None:
    app.router.add_get("", list_workflows)
    app.router.add_post("", create_workflow)
    app.router.add_get("/{workflow_id}", get_workflow)
    app.router.add_patch("/{workflow_id}", update_workflow)
    app.router.add_delete("/{workflow_id}", delete_workflow)
    app.router.add_post("/{workflow_id}/validate", validate_workflow)
    app.router.add_post("/{workflow_id}/test-run", test_run_workflow)
    log.debug("registered workflow routes under %s", prefix)


__all__ = [
    "ROUTE_PREFIX",
    "register_routes",
    "register",
    "list_workflows",
    "create_workflow",
    "get_workflow",
    "update_workflow",
    "delete_workflow",
    "validate_workflow",
    "test_run_workflow",
]
