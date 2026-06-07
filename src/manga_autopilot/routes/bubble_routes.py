"""HTTP routes for speech bubbles."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.models.bubble import SpeechBubble
from manga_autopilot.services.bubble_service import (
    BubbleNotFoundError,
    BubbleService,
)

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects/{project_id}/bubbles"


def _bubble_service(app: Application, project_id: str) -> BubbleService:
    storage_root: Any = app.get("manga_storage_root")
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    project_root = storage_root / "projects" / project_id
    return BubbleService(project_root=project_root)


def _bubble_to_dict(bubble: SpeechBubble) -> dict[str, Any]:
    payload = bubble.model_dump(mode="json")
    return payload


async def _payload(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


async def list_bubbles(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    panel_id = request.query.get("panel_id")
    svc = _bubble_service(request.app, project_id)
    return web.json_response([_bubble_to_dict(b) for b in svc.list_bubbles(panel_id)])


async def create_bubble(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body = await _payload(request)
    try:
        bubble = SpeechBubble.model_validate(body)
    except Exception as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    svc = _bubble_service(request.app, project_id)
    saved = svc.upsert(bubble)
    return web.json_response(_bubble_to_dict(saved), status=201)


async def update_bubble(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    bubble_id = request.match_info["bubble_id"]
    body = await _payload(request)
    body["id"] = bubble_id
    try:
        bubble = SpeechBubble.model_validate(body)
    except Exception as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    svc = _bubble_service(request.app, project_id)
    saved = svc.upsert(bubble)
    return web.json_response(_bubble_to_dict(saved))


async def delete_bubble(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    bubble_id = request.match_info["bubble_id"]
    svc = _bubble_service(request.app, project_id)
    try:
        svc.delete(bubble_id)
    except BubbleNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.Response(status=204)


def register(router) -> None:  # type: ignore[no-untyped-def]
    target = router.router if hasattr(router, "router") else router
    target.add_get(ROUTE_PREFIX, list_bubbles)
    target.add_post(ROUTE_PREFIX, create_bubble)
    target.add_put(f"{ROUTE_PREFIX}/{{bubble_id}}", update_bubble)
    target.add_patch(f"{ROUTE_PREFIX}/{{bubble_id}}", update_bubble)
    target.add_delete(f"{ROUTE_PREFIX}/{{bubble_id}}", delete_bubble)
    log.debug("registered bubble routes under %s", ROUTE_PREFIX)


__all__ = [
    "ROUTE_PREFIX",
    "register",
    "list_bubbles",
    "create_bubble",
    "update_bubble",
    "delete_bubble",
]
