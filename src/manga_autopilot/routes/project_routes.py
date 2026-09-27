"""HTTP routes for project CRUD (spec section 21.2).

Endpoints (all under ``/manga_autopilot/api/projects``):

- ``GET  /projects``          - list project ids
- ``POST /projects``          - create a new project
- ``GET  /projects/{id}``     - fetch the :class:`Project` document
- ``PATCH /projects/{id}``    - update the project document
- ``DELETE /projects/{id}``   - remove the project directory

The endpoints delegate to :class:`ProjectManager`; they only translate
request bodies / HTTP status codes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.services.project_manager import (
    ProjectManager,
    ProjectNotFoundError,
    generate_project_id,
    validate_project_id,
)

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects"


def _storage_root(app: Application) -> Path:
    root = app.get("manga_storage_root")
    if root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    return Path(root)


def _manager(app: Application) -> ProjectManager:
    return ProjectManager(_storage_root(app))


async def _payload(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, Exception) as exc:  # aiohttp JSON error
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


async def list_projects(request: web.Request) -> web.Response:
    manager = _manager(request.app)
    project_ids = manager.list_ids()
    payload: list[dict[str, Any]] = []
    for pid in project_ids:
        try:
            project = manager.load(pid)
        except ProjectNotFoundError:
            continue
        payload.append(project.model_dump(mode="json"))
    return web.json_response(payload)


async def create_project(request: web.Request) -> web.Response:
    body = await _payload(request)
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise web.HTTPBadRequest(text="name is required")
    idea = body.get("idea") if isinstance(body.get("idea"), str) else None
    title = body.get("title") if isinstance(body.get("title"), str) else None
    language = body.get("language", "ja") if isinstance(body.get("language", "ja"), str) else "ja"
    project_id = body.get("id")
    if project_id is not None:
        if not isinstance(project_id, str) or not project_id:
            raise web.HTTPBadRequest(text="id must be a non-empty string")
        try:
            validate_project_id(project_id)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
    manager = _manager(request.app)
    try:
        project = manager.create(
            name=name,
            idea=idea,
            title=title,
            language=language,
            project_id=project_id,
        )
    except FileExistsError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    return web.json_response(project.model_dump(mode="json"), status=201)


async def get_project(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    manager = _manager(request.app)
    try:
        project = manager.load(project_id)
    except ProjectNotFoundError as exc:
        raise web.HTTPNotFound(text=f"project {project_id} not found") from exc
    return web.json_response(project.model_dump(mode="json"))


async def patch_project(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body = await _payload(request)
    manager = _manager(request.app)
    try:
        project = manager.load(project_id)
    except ProjectNotFoundError as exc:
        raise web.HTTPNotFound(text=f"project {project_id} not found") from exc

    # Apply supported patch fields.  ``name`` / ``title`` / ``idea`` /
    # ``language`` / ``status`` are simple scalar updates; ``settings``
    # is replaced wholesale if supplied.
    if "name" in body and isinstance(body["name"], str):
        project.name = body["name"]
    if "title" in body:
        title = body["title"]
        project.title = title if isinstance(title, str) or title is None else project.title
    if "idea" in body:
        idea = body["idea"]
        project.idea = idea if isinstance(idea, str) or idea is None else project.idea
    if "language" in body and isinstance(body["language"], str):
        project.language = body["language"]
    if "status" in body and isinstance(body["status"], str):
        project.status = body["status"]
    if "generation_profile_id" in body and isinstance(body["generation_profile_id"], str):
        # Selecting an Anima profile is what turns on strict behaviour and the
        # review gates, so it has to be patchable.
        project.generation_profile_id = body["generation_profile_id"]
    if "license_acknowledged" in body and isinstance(body["license_acknowledged"], bool):
        project.license_acknowledged = body["license_acknowledged"]
    if "settings" in body and isinstance(body["settings"], dict):
        try:
            project.settings = project.settings.model_validate(body["settings"])
        except Exception as exc:  # pydantic ValidationError
            raise web.HTTPBadRequest(text=f"invalid settings: {exc}") from exc

    saved = manager.save(project)
    return web.json_response(saved.model_dump(mode="json"))


async def delete_project(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    manager = _manager(request.app)
    try:
        manager.delete(project_id)
    except ProjectNotFoundError as exc:
        raise web.HTTPNotFound(text=f"project {project_id} not found") from exc
    return web.json_response({"deleted": project_id})


async def suggest_project_id(_request: web.Request) -> web.Response:
    """Return a freshly generated project id (handy for the UI)."""

    return web.json_response({"id": generate_project_id()})


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_get(ROUTE_PREFIX, list_projects)
    router.add_post(ROUTE_PREFIX, create_project)
    router.add_get(ROUTE_PREFIX + "/{project_id}", get_project)
    router.add_patch(ROUTE_PREFIX + "/{project_id}", patch_project)
    router.add_delete(ROUTE_PREFIX + "/{project_id}", delete_project)
    router.add_get(ROUTE_PREFIX + "/_suggest_id", suggest_project_id)


__all__ = [
    "ROUTE_PREFIX",
    "create_project",
    "delete_project",
    "get_project",
    "list_projects",
    "patch_project",
    "register",
    "suggest_project_id",
]
