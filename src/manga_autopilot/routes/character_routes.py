"""HTTP routes for character management (spec section 22)."""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.models.character import Character
from manga_autopilot.services.character_service import (
    CharacterNotFoundError,
    CharacterService,
    CharacterValidationError,
)

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects/{project_id}/characters"


def _service(app: Application, project_id: str) -> CharacterService:
    storage_root: Any = app.get("manga_storage_root")
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    return CharacterService.for_project(storage_root, project_id)


async def _payload(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


async def list_characters(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    svc = _service(request.app, project_id)
    return web.json_response([c.model_dump(mode="json") for c in svc.list()])


async def get_character(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    character_id = request.match_info["character_id"]
    svc = _service(request.app, project_id)
    try:
        char = svc.get(character_id)
    except CharacterNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(char.model_dump(mode="json"))


async def create_character(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body = await _payload(request)
    svc = _service(request.app, project_id)
    try:
        char = Character.model_validate(body)
        svc.create(char)
    except (CharacterValidationError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response(char.model_dump(mode="json"), status=201)


async def update_character(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    character_id = request.match_info["character_id"]
    body = await _payload(request)
    svc = _service(request.app, project_id)
    try:
        char = svc.update(character_id, body)
    except CharacterNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    except (CharacterValidationError, ValueError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response(char.model_dump(mode="json"))


async def delete_character(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    character_id = request.match_info["character_id"]
    svc = _service(request.app, project_id)
    try:
        svc.delete(character_id)
    except CharacterNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response({"deleted": character_id})


async def upload_reference(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    character_id = request.match_info["character_id"]
    body = await _payload(request)
    data_field = body.get("data_base64")
    filename = body.get("filename", "reference.png")
    label = body.get("label", "")
    if not data_field:
        raise web.HTTPBadRequest(text="data_base64 is required")
    try:
        raw = base64.b64decode(data_field, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise web.HTTPBadRequest(text=f"invalid base64: {exc}") from exc
    svc = _service(request.app, project_id)
    try:
        upload = svc.register_reference_image(character_id, filename, raw, label=label)
    except CharacterNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    except CharacterValidationError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response(
        {
            "asset_ref": upload.asset_ref.model_dump(),
            "width": upload.width,
            "height": upload.height,
            "bytes": upload.bytes_written,
        },
        status=201,
    )


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_get(ROUTE_PREFIX, list_characters)
    router.add_get(ROUTE_PREFIX + "/{character_id}", get_character)
    router.add_post(ROUTE_PREFIX, create_character)
    router.add_put(ROUTE_PREFIX + "/{character_id}", update_character)
    router.add_delete(ROUTE_PREFIX + "/{character_id}", delete_character)
    router.add_post(ROUTE_PREFIX + "/{character_id}/references", upload_reference)


__all__ = ["register", "ROUTE_PREFIX"]
