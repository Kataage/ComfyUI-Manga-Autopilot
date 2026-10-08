"""Read-only v2 Work Export Center discovery and verified PNG access.

Exports are immutable Artifact rows registered by WorkPageExportService.
Never enumerate arbitrary filesystem paths or accept client-supplied paths.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from aiohttp import web

from manga_autopilot.repositories import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRepository,
    WorkIdentityMismatchError,
    WorkNotFoundError,
)
from manga_autopilot.storage import assert_managed_path, repository_read
from manga_autopilot.storage.paths import UnsafeStoragePathError

ROUTE_PREFIX = "/manga_autopilot/api/v2/works/{work_id}/exports"


def _repository(request: web.Request) -> ArtifactRepository:
    root: Any = request.app.get("manga_storage_root")
    if root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    return ArtifactRepository(Path(root), request.match_info["work_id"])


def _error(exc: Exception) -> web.Response:
    if isinstance(exc, (WorkNotFoundError, ArtifactNotFoundError)):
        return web.json_response(
            {"error": "not_found", "message": str(exc)}, status=404
        )
    if isinstance(exc, WorkIdentityMismatchError):
        return web.json_response(
            {"error": "ownership_conflict", "message": str(exc)}, status=409
        )
    if isinstance(exc, ArtifactIntegrityError):
        return web.json_response(
            {"error": "export_file_invalid", "message": str(exc)}, status=422
        )
    if isinstance(exc, (ValueError, UnsafeStoragePathError)):
        return web.json_response(
            {"error": "invalid_request", "message": str(exc)}, status=400
        )
    raise exc


def _present(row: dict[str, Any]) -> dict[str, Any]:
    return {
        field: row[field]
        for field in (
            "id", "artifact_type", "scope_type", "scope_id", "relative_path",
            "mime_type", "sha256", "file_size", "width", "height",
            "dependency_fingerprint", "created_at", "status",
        )
    }


def _ensure_page_png(row: dict[str, Any], artifact_id: str) -> None:
    if (
        row["artifact_type"] != "page_render"
        or row["scope_type"] != "page"
        or not row["scope_id"]
        or row["mime_type"] != "image/png"
        or row["status"] != "READY"
        or row["archived_at"] is not None
    ):
        raise ArtifactNotFoundError(
            f"no current Work Page PNG export with Artifact ID {artifact_id!r}"
        )


async def list_work_exports(request: web.Request) -> web.Response:
    try:
        repository = _repository(request)
        handle = repository._open()
        with repository_read(handle.database_path) as conn:
            rows = [
                dict(row) for row in conn.execute(
                    """SELECT * FROM artifacts
                    WHERE artifact_type = 'page_render'
                      AND scope_type = 'page'
                      AND mime_type = 'image/png'
                      AND status = 'READY'
                      AND archived_at IS NULL
                    ORDER BY created_commit_seq DESC, id DESC"""
                )
            ]
        return web.json_response({
            "work_id": request.match_info["work_id"],
            "exports": [_present(row) for row in rows],
        })
    except Exception as exc:
        return _error(exc)


async def get_work_export_png(request: web.Request) -> web.Response:
    try:
        repository = _repository(request)
        artifact_id = request.match_info["artifact_id"]
        row = repository.get(artifact_id)
        _ensure_page_png(row, artifact_id)
        repository.verify_registered_file(artifact_id)
        handle = repository._open()
        path = assert_managed_path(
            handle.root.joinpath(*row["relative_path"].split("/")),
            containment_root=handle.root,
            field_name="Work Page PNG export",
        )
        if path.is_symlink() or not path.is_file():
            raise ArtifactIntegrityError(
                f"registered Work PNG {artifact_id!r} is missing or unsafe"
            )
        data = path.read_bytes()
        if len(data) != row["file_size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ArtifactIntegrityError(
                f"registered Work PNG {artifact_id!r} changed while reading"
            )
        return web.Response(
            body=data,
            content_type="image/png",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except Exception as exc:
        return _error(exc)


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_get(ROUTE_PREFIX, list_work_exports)
    router.add_get(ROUTE_PREFIX + "/{artifact_id}/png", get_work_export_png)


__all__ = [
    "ROUTE_PREFIX",
    "get_work_export_png",
    "list_work_exports",
    "register",
]
