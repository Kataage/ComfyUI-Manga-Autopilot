"""v2 Work Page PNG export: server-resolved Layout/Panel/Artifact state only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from manga_autopilot.repositories import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    PageDomainNotFoundError,
    WorkIdentityMismatchError,
    WorkNotFoundError,
)
from manga_autopilot.services.work_page_export import (
    PageExportConflictError,
    PageExportValidationError,
    WorkPageExportService,
)
from manga_autopilot.storage.paths import UnsafeStoragePathError

ROUTE_PREFIX = "/manga_autopilot/api/v2/works/{work_id}/pages/{page_id}/export/png"
_ALLOWED_SETTINGS = frozenset({"background", "outer_border"})


async def export_work_page_png(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (ValueError, TypeError) as exc:
        return web.json_response(
            {"error": "invalid_request", "message": f"invalid JSON: {exc}"},
            status=400,
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "invalid_request", "message": "body must be a JSON object"},
            status=400,
        )
    unknown = set(body) - _ALLOWED_SETTINGS
    if unknown:
        return web.json_response(
            {
                "error": "invalid_request",
                "message": f"layout, Panel paths and unsupported settings not accepted: {sorted(unknown)}",
            },
            status=400,
        )
    storage_root: Any = request.app.get("manga_storage_root")
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    try:
        result = WorkPageExportService(Path(storage_root)).export_png(
            request.match_info["work_id"], request.match_info["page_id"], **body
        )
    except (PageDomainNotFoundError, WorkNotFoundError) as exc:
        return web.json_response(
            {"error": "not_found", "message": str(exc)}, status=404
        )
    except PageExportConflictError as exc:
        return web.json_response(
            {"error": "page_changed", "message": str(exc)}, status=409
        )
    except WorkIdentityMismatchError as exc:
        return web.json_response(
            {"error": "ownership_conflict", "message": str(exc)}, status=409
        )
    except (PageExportValidationError, ArtifactNotFoundError, ArtifactIntegrityError) as exc:
        return web.json_response(
            {"error": "export_precondition_failed", "message": str(exc)}, status=422
        )
    except (ValueError, UnsafeStoragePathError) as exc:
        return web.json_response(
            {"error": "invalid_request", "message": str(exc)}, status=400
        )
    return web.json_response(result, status=201)


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_post(ROUTE_PREFIX, export_work_page_png)


__all__ = ["ROUTE_PREFIX", "export_work_page_png", "register"]
