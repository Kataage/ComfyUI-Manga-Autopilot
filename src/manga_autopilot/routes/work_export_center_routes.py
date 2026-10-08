"""Read-only v2 Work Export Center discovery and verified PNG access.

Exports are immutable Artifact rows registered by WorkPageExportService.
Never enumerate arbitrary filesystem paths or accept client-supplied paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
import threading
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
from manga_autopilot.services.page_export_freshness import page_png_freshness
from manga_autopilot.services.page_png_budget import (
    MAX_SERVABLE_PNG_BYTES,
    PNG_IO_CHUNK_BYTES,
    PNG_SPOOL_MEMORY_BYTES,
)
from manga_autopilot.storage import assert_managed_path, repository_read
from manga_autopilot.storage.paths import UnsafeStoragePathError

ROUTE_PREFIX = "/manga_autopilot/api/v2/works/{work_id}/exports"
# Cap disk-spool concurrency. Each download uses at most 1 MiB Python RAM
# plus a bounded, checked on-disk temp snapshot, not a full in-memory PNG.
_DOWNLOAD_SLOTS = threading.BoundedSemaphore(value=2)


def _verified_png_snapshot(path: Path, row: dict[str, Any]):
    """Copy and verify one pinned input file descriptor before serving bytes.

    The returned file-like object is an independently owned, seeked snapshot,
    not a live path. Replacement or mutation of the Work path after this
    function cannot alter the emitted response body. Only verified bytes may
    reach the client; never stream a partial unverified corrupt artifact.
    """
    limit = MAX_SERVABLE_PNG_BYTES
    expected_size = row["file_size"]
    if type(expected_size) is not int or not 0 < expected_size <= limit:
        raise ArtifactIntegrityError(
            f"registered Work PNG {row['id']!r} exceeds the {limit:,} byte "
            "download budget; use a smaller export"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"registered Work PNG {row['id']!r} file is missing or unsafe: {exc}"
        ) from exc
    snapshot = tempfile.SpooledTemporaryFile(max_size=PNG_SPOOL_MEMORY_BYTES)
    try:
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ArtifactIntegrityError("registered Work PNG is not a regular file")
            digest = hashlib.sha256()
            length = 0
            while chunk := source.read(PNG_IO_CHUNK_BYTES):
                length += len(chunk)
                if length > limit or length > expected_size:
                    raise ArtifactIntegrityError(
                        f"registered Work PNG {row['id']!r} exceeds its size budget"
                    )
                digest.update(chunk)
                snapshot.write(chunk)
        if length != expected_size or digest.hexdigest() != row["sha256"]:
            raise ArtifactIntegrityError(
                f"registered Work PNG {row['id']!r} hash/size mismatch"
            )
        snapshot.seek(0)
        return snapshot
    except BaseException:
        snapshot.close()
        raise



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


def _present(row: dict[str, Any], freshness: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            field: row[field]
            for field in (
                "id", "artifact_type", "scope_type", "scope_id", "relative_path",
                "mime_type", "sha256", "file_size", "width", "height",
                "dependency_fingerprint", "created_at", "status",
            )
        },
        **freshness,
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
            # The artifacts and their later invalidations share one snapshot.
            conn.execute("BEGIN")
            try:
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
                exports = [
                    _present(row, page_png_freshness(conn, row)) for row in rows
                ]
            finally:
                conn.rollback()
        return web.json_response({
            "work_id": request.match_info["work_id"],
            "exports": exports,
        })
    except Exception as exc:
        return _error(exc)


async def get_work_export_png(request: web.Request) -> web.Response:
    try:
        repository = _repository(request)
        artifact_id = request.match_info["artifact_id"]
        require_current = request.query.get("require_current", "0")
        if require_current not in {"0", "1"}:
            raise ValueError("require_current must be 0 or 1")
        handle = repository._open()
        with repository_read(handle.database_path) as conn:
            conn.execute("BEGIN")
            try:
                record = conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
                if record is None:
                    raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
                row = dict(record)
                _ensure_page_png(row, artifact_id)
                freshness = page_png_freshness(conn, row)
            finally:
                conn.rollback()
        # Historical downloads remain available. Consumers explicitly requiring
        # fresh output fail closed, without converting READY into a stale status.
        if require_current == "1" and not freshness["is_current"]:
            return web.json_response(
                {
                    "error": "stale_export",
                    "message": (
                        f"Page PNG {artifact_id!r} is {freshness['freshness']}; "
                        "export the current saved Page again."
                    ),
                    **freshness,
                },
                status=409,
            )
        if not _DOWNLOAD_SLOTS.acquire(blocking=False):
            return web.json_response(
                {"error": "download_busy", "message":
                 "Work PNG download budget is busy; retry after other downloads finish"},
                status=429, headers={"Retry-After": "2"},
            )
        try:
            path = assert_managed_path(
                handle.root.joinpath(*row["relative_path"].split("/")),
                containment_root=handle.root,
                field_name="Work Page PNG export",
            )
            # Validate a pinned original file descriptor and copy in bounded
            # chunks. Stream only from the verified independent temp snapshot.
            snapshot = await asyncio.to_thread(_verified_png_snapshot, path, row)
            try:
                response = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "image/png",
                        "Cache-Control": "private, no-store",
                        "X-Content-Type-Options": "nosniff",
                        "X-Work-Export-Freshness": freshness["freshness"],
                    },
                )
                response.content_length = row["file_size"]
                await response.prepare(request)
                while chunk := await asyncio.to_thread(snapshot.read, PNG_IO_CHUNK_BYTES):
                    await response.write(chunk)
                await response.write_eof()
                return response
            finally:
                snapshot.close()
        finally:
            _DOWNLOAD_SLOTS.release()
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
