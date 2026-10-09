"""v2 Work Page PNG export: server-resolved Layout/Panel/Artifact state only."""

from __future__ import annotations

import asyncio
import threading
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
    PageExportBusyError,
    PageExportConflictError,
    PageExportValidationError,
    WorkPageExportService,
)
from manga_autopilot.storage.paths import UnsafeStoragePathError
from manga_autopilot.storage.repository import WorkMutationLeaseConflictError

ROUTE_PREFIX = "/manga_autopilot/api/v2/works/{work_id}/pages/{page_id}/export/png"
_ALLOWED_SETTINGS = frozenset({"background", "outer_border", "export_profile"})


async def _run_page_export(
    service: WorkPageExportService,
    work_id: str,
    page_id: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Keep Pillow/file IO off the HTTP event loop and own worker cancellation.

    Cancelling an asyncio.to_thread waiter does not stop its thread. We signal
    cancellation to the Work export's pre-publication and commit-time guards,
    then drain the worker before propagating cancellation. The service's render
    semaphore belongs to the worker and is never released early.
    """
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(
        service.export_png, work_id, page_id, cancel_event=cancelled, **settings,
    ))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        # Cancellation is repeatable: a second task.cancel() while this
        # coroutine drains the thread must not detach the still-running
        # renderer. Keep the worker owned until its temp files and render
        # semaphore have been released (the worker owns both).
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # A subsequent cancellation targets this HTTP task, not the
                # protected worker. Do not return while its IO is in flight.
                continue
            except Exception:
                # A failed worker is finished; preserve the original caller
                # cancellation instead of converting it to an HTTP error.
                break
        if worker.done() and not worker.cancelled():
            # Observe a late worker failure even if repeated cancellations
            # raced with completion; avoids an unhandled-task-exception log.
            worker.exception()
        raise


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
        result = await _run_page_export(
            WorkPageExportService(Path(storage_root)),
            request.match_info["work_id"], request.match_info["page_id"], body,
        )
    except (PageDomainNotFoundError, WorkNotFoundError) as exc:
        return web.json_response(
            {"error": "not_found", "message": str(exc)}, status=404
        )
    except PageExportBusyError as exc:
        return web.json_response(
            {"error": "export_busy", "message": str(exc)}, status=429,
            headers={"Retry-After": "2"},
        )
    except WorkMutationLeaseConflictError as exc:
        return web.json_response(
            {"error": "work_mutation_busy", "message": str(exc)}, status=409
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
