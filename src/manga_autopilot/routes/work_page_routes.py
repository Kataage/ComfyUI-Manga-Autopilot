"""v2 Work-authoritative Page snapshot and layout command HTTP endpoints.

No legacy project JSON services are involved. The Work lifecycle repository
validates catalog, identity and migration state before any database access.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from aiohttp import web

from manga_autopilot.repositories.page_domain import (
    PageDomainArchivedError,
    PageDomainNotFoundError,
    PageDomainOwnershipError,
    PageDomainPanelArchivedError,
)
from manga_autopilot.repositories.work_lifecycle import (
    WorkIdentityMismatchError,
    WorkNotFoundError,
)
from manga_autopilot.services.page_application import PageApplicationService
from manga_autopilot.storage.paths import UnsafeStoragePathError
from manga_autopilot.storage.repository import RevisionConflictError

ROUTE_PREFIX = "/manga_autopilot/api/v2/works/{work_id}/pages"
_LAYOUT_FIELDS = frozenset({
    "source_template_id", "template_snapshot_id", "layout_kind",
    "reading_direction", "parameters_json", "geometry_json", "constraints_json",
})


def _service(request: web.Request) -> PageApplicationService:
    root = request.app.get("manga_storage_root")
    if root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    return PageApplicationService(Path(root))


def _problem(status: int, error: str, message: str, **details: Any) -> web.Response:
    return web.json_response(
        {"error": error, "message": message, **details}, status=status
    )


def _translate(exc: Exception) -> web.Response:
    if isinstance(exc, RevisionConflictError):
        return _problem(
            409, "revision_conflict", str(exc),
            entity_type=exc.entity_type,
            entity_id=exc.entity_id,
            expected_revision=exc.expected_revision,
            actual_revision=exc.actual_revision,
        )
    if isinstance(exc, PageDomainArchivedError):
        return _problem(409, "page_archived", str(exc))
    if isinstance(exc, PageDomainPanelArchivedError):
        return _problem(409, "panel_archived", str(exc))
    if isinstance(exc, (PageDomainNotFoundError, WorkNotFoundError)):
        return _problem(404, "not_found", str(exc))
    if isinstance(exc, (PageDomainOwnershipError, WorkIdentityMismatchError)):
        return _problem(409, "ownership_conflict", str(exc))
    if isinstance(exc, sqlite3.IntegrityError):
        return _problem(409, "constraint_conflict", str(exc))
    if isinstance(exc, (ValueError, UnsafeStoragePathError)):
        return _problem(400, "invalid_request", str(exc))
    raise exc


async def _payload(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(
            text=f"invalid JSON body: {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


def _include_archived(request: web.Request) -> bool:
    value = request.query.get("include_archived", "0")
    if value not in {"0", "1"}:
        raise ValueError("include_archived must be 0 or 1")
    return value == "1"


async def list_work_pages(request: web.Request) -> web.Response:
    try:
        pages = _service(request).list_pages(
            request.match_info["work_id"], include_archived=_include_archived(request)
        )
    except Exception as exc:
        return _translate(exc)
    return web.json_response({"pages": pages})


async def get_work_page(request: web.Request) -> web.Response:
    try:
        state = _service(request).get_page(
            request.match_info["work_id"], request.match_info["page_id"],
            include_archived=_include_archived(request),
        )
    except Exception as exc:
        return _translate(exc)
    return web.json_response(state)


async def update_work_page_layout(request: web.Request) -> web.Response:
    body = await _payload(request)
    expected_revision = body.pop("expected_revision", None)
    if type(expected_revision) is not int or expected_revision < 1:
        return _problem(
            400, "invalid_request", "expected_revision must be a positive integer"
        )
    slot_updates = body.pop("slot_updates", [])
    panel_bindings = body.pop("panel_bindings", [])
    if not isinstance(slot_updates, list) or not all(
        isinstance(x, dict) for x in slot_updates
    ):
        return _problem(400, "invalid_request", "slot_updates must be a list of objects")
    if not isinstance(panel_bindings, list) or not all(
        isinstance(x, dict) for x in panel_bindings
    ):
        return _problem(
            400, "invalid_request", "panel_bindings must be a list of objects"
        )
    unexpected = set(body) - _LAYOUT_FIELDS
    if unexpected:
        return _problem(
            400, "invalid_request", f"unsupported layout fields: {sorted(unexpected)}"
        )
    try:
        state = _service(request).update_layout(
            request.match_info["work_id"], request.match_info["page_id"],
            expected_revision=expected_revision,
            slot_updates=slot_updates,
            panel_bindings=panel_bindings,
            **body,
        )
    except Exception as exc:
        return _translate(exc)
    return web.json_response(state)


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_get(ROUTE_PREFIX, list_work_pages)
    router.add_get(ROUTE_PREFIX + "/{page_id}", get_work_page)
    router.add_patch(ROUTE_PREFIX + "/{page_id}/layout", update_work_page_layout)


__all__ = [
    "ROUTE_PREFIX",
    "get_work_page",
    "list_work_pages",
    "register",
    "update_work_page_layout",
]
