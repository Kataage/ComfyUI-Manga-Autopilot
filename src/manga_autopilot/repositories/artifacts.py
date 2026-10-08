"""Work-local, immutable artifact publication and revisioned SQLite provenance.

A filesystem and SQLite cannot share one atomic transaction. Publish the
validated bytes first, then register metadata and entity history in one short
Work transaction. A failed registration may leave an orphan final file, but
never a committed row pointing at a file that was not published.
"""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from PIL import Image, UnidentifiedImageError

from manga_autopilot.primitives import new_id
from manga_autopilot.repositories.work_lifecycle import WorkLifecycleRepository
from manga_autopilot.storage import (
    assert_managed_path,
    create_work_commit,
    create_work_entity_revision,
    repository_read,
    repository_write,
    validate_work_id,
)

_IMAGE_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}
_READ_CHUNK_BYTES = 1024 * 1024


class ArtifactNotFoundError(LookupError):
    """An Artifact ID does not exist in the authoritative Work DB."""


class ArtifactIntegrityError(ValueError):
    """Artifact is empty, is not the declared media type, or has changed."""


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _valid_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("relative_path must be a non-empty portable POSIX path")
    if value.startswith("/") or "//" in value or value.endswith("/"):
        raise ValueError("relative_path must be a canonical Work-relative path")
    parts = value.split("/")
    if len(parts) < 2 or parts[0] not in {"assets", "exports"}:
        raise ValueError("relative_path must be inside Work assets/ or exports/")
    if parts[:2] == ["assets", "temp"]:
        raise ValueError("assets/temp is reserved for unregistered temporary files")
    for part in parts:
        validate_work_id(part)
    candidate = PurePosixPath(value)
    if candidate.as_posix() != value:
        raise ValueError("relative_path must be canonical")
    return candidate


def _fsync_dir(directory: Path) -> None:
    # Filesystem directory fsync is unavailable on Windows; data fsync still
    # happens on every platform before the file is made visible.
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_media(path: Path, mime_type: str) -> tuple[int | None, int | None]:
    if path.stat().st_size <= 0:
        raise ArtifactIntegrityError("artifact cannot be empty")
    if mime_type.startswith("image/"):
        expected = _IMAGE_FORMATS.get(mime_type)
        if expected is None:
            raise ArtifactIntegrityError(f"unsupported image media type: {mime_type}")
        try:
            with Image.open(path) as img:
                if img.format != expected:
                    raise ArtifactIntegrityError(
                        f"image format {img.format!r} does not match {mime_type!r}"
                    )
                width, height = img.size
                img.verify()
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise ArtifactIntegrityError("invalid image artifact") from exc
        return width, height
    return None, None


def _copy_and_sync(stream: BinaryIO, target: Path) -> None:
    # Temporary destination was created using O_EXCL in the same Work volume.
    with target.open("wb") as output:
        while True:
            piece = stream.read(_READ_CHUNK_BYTES)
            if not piece:
                break
            output.write(piece)
        output.flush()
        os.fsync(output.fileno())


def _fingerprint_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as source:
        while piece := source.read(_READ_CHUNK_BYTES):
            digest.update(piece)
            count += len(piece)
    return digest.hexdigest(), count


def _publish_exclusive(temp: Path, destination: Path) -> None:
    """Atomically publish without replacing an existing immutable Artifact.

    os.replace()/POSIX os.rename() can overwrite a pre-existing path.
    An exclusive same-filesystem hard link is an atomic, non-clobbering
    publication primitive; unlinking the temp name then completes the move.
    If interrupted, the temp and/or orphan final file is recoverable.
    """
    os.link(temp, destination)
    _fsync_dir(destination.parent)
    temp.unlink()
    _fsync_dir(temp.parent)


# A verified-page-render Work commit is an immutable attestation minted only
# after the exporter checks its exact Page/Panel/Candidate source projection
# under the Artifact publication transaction. Generic imports never mint it.
VERIFIED_PAGE_RENDER_OPERATION = "register_verified_page_render_v1"


def verified_page_render_reason(
    artifact_id: str, page_id: str, fingerprint: str,
) -> str:
    """Bind a Page export's artifact identity, owner and input digest."""
    return f"page_export_attestation_v1:{artifact_id}:{page_id}:{fingerprint}"


class ArtifactRepository:
    """Register immutable local Artifact files against one validated Work DB.

    Instantiate with the storage root and Work ID (not a legacy Project ID);
    the Work lifecycle validates/migrates the portable Work before each write.
    Existing ArtifactStore Local/S3 upload protocols are not modified.
    """

    def __init__(self, storage_root: str | Path, work_id: str) -> None:
        self.storage_root = Path(storage_root)
        self.work_id = validate_work_id(work_id)

    def _open(self):
        return WorkLifecycleRepository(self.storage_root).open_work(self.work_id)

    def get(self, artifact_id: str) -> dict[str, Any]:
        handle = self._open()
        with repository_read(handle.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        return dict(row)

    def list_for_scope(self, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        handle = self._open()
        with repository_read(handle.database_path) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM artifacts
                    WHERE scope_type = ? AND scope_id = ?
                    ORDER BY created_commit_seq, id""",
                    (scope_type, scope_id),
                )
            ]

    def verify_registered_file(self, artifact_id: str) -> dict[str, Any]:
        """Verify current on-disk presence/hash without mutating a historical row."""
        row = self.get(artifact_id)
        handle = self._open()
        relative = _valid_relative_path(row["relative_path"])
        path = assert_managed_path(
            handle.root.joinpath(*relative.parts),
            containment_root=handle.root,
            field_name="registered artifact",
        )
        if not path.is_file():
            raise ArtifactIntegrityError(f"registered artifact file missing: {relative}")
        sha, size = _fingerprint_file(path)
        if sha != row["sha256"] or size != row["file_size"]:
            raise ArtifactIntegrityError(f"registered artifact hash/size mismatch: {relative}")
        return row

    def register_local_bytes(
        self,
        *,
        data: bytes,
        relative_path: str,
        artifact_type: str,
        dependency_fingerprint: str,
        artifact_id: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        mime_type: str = "application/octet-stream",
        run_id: str | None = None,
        generation_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        return self._register_stream(
            io.BytesIO(data),
            relative_path=relative_path,
            artifact_type=artifact_type,
            dependency_fingerprint=dependency_fingerprint,
            artifact_id=artifact_id,
            scope_type=scope_type,
            scope_id=scope_id,
            mime_type=mime_type,
            run_id=run_id,
            generation_attempt_id=generation_attempt_id,
        )

    def register_local_file(
        self,
        *,
        source_path: str | Path,
        relative_path: str,
        artifact_type: str,
        dependency_fingerprint: str,
        artifact_id: str | None = None,
        scope_type: str | None = None,
        scope_id: str | None = None,
        mime_type: str = "application/octet-stream",
        run_id: str | None = None,
        generation_attempt_id: str | None = None,
        commit_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> dict[str, Any]:
        source = Path(source_path)
        if source.is_symlink() or not source.is_file():
            raise ValueError("source_path must be an existing regular non-symlink file")
        with source.open("rb") as handle:
            return self._register_stream(
                handle,
                relative_path=relative_path,
                artifact_type=artifact_type,
                dependency_fingerprint=dependency_fingerprint,
                artifact_id=artifact_id,
                scope_type=scope_type,
                scope_id=scope_id,
                mime_type=mime_type,
                run_id=run_id,
                generation_attempt_id=generation_attempt_id,
                commit_guard=commit_guard,
            )

    def _register_verified_page_render_file(
        self,
        *,
        source_path: str | Path,
        relative_path: str,
        page_id: str,
        dependency_fingerprint: str,
        artifact_id: str,
        commit_guard: Callable[[sqlite3.Connection], None],
    ) -> dict[str, Any]:
        """Internal exporter-only registration of a source-guarded Page PNG.

        Call exclusively after WorkPageExportService prepared its persisted
        source snapshot and final BEGIN IMMEDIATE revision/candidate guard.
        The generic public register_local_file/bytes APIs NEVER create a
        trusted provenance commit, even when their caller supplies a guard.
        """
        if not callable(commit_guard):
            raise ValueError("verified Page render requires a commit guard")
        if (
            len(dependency_fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in dependency_fingerprint)
        ):
            raise ValueError("verified Page render requires canonical SHA256 fingerprint")
        source = Path(source_path)
        if source.is_symlink() or not source.is_file():
            raise ValueError("source_path must be an existing regular non-symlink file")
        with source.open("rb") as handle:
            return self._register_stream(
                handle,
                relative_path=relative_path,
                artifact_type="page_render",
                dependency_fingerprint=dependency_fingerprint,
                artifact_id=artifact_id,
                scope_type="page",
                scope_id=page_id,
                mime_type="image/png",
                run_id=None,
                generation_attempt_id=None,
                commit_guard=commit_guard,
                _verified_page_render=True,
            )

    def _register_stream(
        self,
        stream: BinaryIO,
        *,
        relative_path: str,
        artifact_type: str,
        dependency_fingerprint: str,
        artifact_id: str | None,
        scope_type: str | None,
        scope_id: str | None,
        mime_type: str,
        run_id: str | None,
        generation_attempt_id: str | None,
        commit_guard: Callable[[sqlite3.Connection], None] | None = None,
        _verified_page_render: bool = False,
    ) -> dict[str, Any]:
        relative = _valid_relative_path(relative_path)
        kind = _nonempty(artifact_type, "artifact_type")
        fingerprint = _nonempty(dependency_fingerprint, "dependency_fingerprint")
        mime = _nonempty(mime_type, "mime_type")
        artifact_key = _nonempty(artifact_id or new_id("artifact"), "artifact_id")
        if (scope_type is None) != (scope_id is None):
            raise ValueError("scope_type and scope_id must be provided together")
        if scope_type is not None:
            _nonempty(scope_type, "scope_type")
            _nonempty(scope_id, "scope_id")
        if run_id is not None:
            _nonempty(run_id, "run_id")
        if generation_attempt_id is not None:
            _nonempty(generation_attempt_id, "generation_attempt_id")

        work = self._open()
        target = assert_managed_path(
            work.root.joinpath(*relative.parts),
            containment_root=work.root,
            field_name="artifact final path",
        )
        temp_root = assert_managed_path(
            work.root / "assets" / "temp",
            containment_root=work.root,
            field_name="artifact temporary directory",
        )
        # Final and temp locations share the Work filesystem for atomic
        # publication. Every path component is checked against symlinks.
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_root.mkdir(parents=True, exist_ok=True)
        target = assert_managed_path(
            target, containment_root=work.root, field_name="artifact final path"
        )
        temp_root = assert_managed_path(
            temp_root, containment_root=work.root, field_name="artifact temporary directory"
        )
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"immutable artifact path already exists: {relative}")

        fd, temp_name = tempfile.mkstemp(prefix="artifact-", suffix=".tmp", dir=temp_root)
        os.close(fd)
        temp = Path(temp_name)
        try:
            _copy_and_sync(stream, temp)
            width, height = _validate_media(temp, mime)
            digest, size = _fingerprint_file(temp)
            assert_managed_path(
                target, containment_root=work.root, field_name="artifact final path"
            )
            # A failed file publication must never reach the DB transaction.
            _publish_exclusive(temp, target)
        finally:
            # Only uncommitted temporary files are cleaned. Published final
            # files are left as orphans if the later DB transaction fails.
            if temp.exists():
                temp.unlink()

        # The committed DB row always follows the completed final file.
        # A constraint error leaves the published orphan for maintenance.
        with repository_write(work.database_path) as conn:
            if not target.is_file():
                raise ArtifactIntegrityError("published artifact disappeared before registration")
            # The caller's DB-only guard checks all source revisions under
            # this SAME BEGIN IMMEDIATE Work transaction. A mismatch aborts
            # before any commit/revision/READY Artifact row is written.
            # The already-published immutable file remains a recoverable orphan.
            if commit_guard is not None:
                commit_guard(conn)
            # Attestation and Artifact row MUST share one atomic Work commit.
            # The private verified path is used only after the complete source
            # validation callback succeeds inside this BEGIN IMMEDIATE lock.
            commit = create_work_commit(
                conn,
                commit_id=new_id("commit"),
                actor_type="system",
                operation_type=(
                    VERIFIED_PAGE_RENDER_OPERATION
                    if _verified_page_render else "register_artifact"
                ),
                reason=(
                    verified_page_render_reason(
                        artifact_key, scope_id, fingerprint,
                    )
                    if _verified_page_render else f"publish {kind}"
                ),
            )
            conn.execute(
                """INSERT INTO artifacts (
                    id, artifact_type, scope_type, scope_id, relative_path,
                    mime_type, sha256, file_size, width, height, run_id,
                    generation_attempt_id, revision, dependency_fingerprint,
                    status, created_commit_seq, created_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact_key, kind, scope_type, scope_id, relative.as_posix(),
                    mime, digest, size, width, height, run_id,
                    generation_attempt_id, 1, fingerprint, "READY",
                    commit.commit_seq, commit.created_at, None,
                ),
            )
            row = dict(conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_key,)
            ).fetchone())
            create_work_entity_revision(
                conn,
                revision_id=new_id("revision"),
                entity_type="artifact",
                entity_id=artifact_key,
                entity_revision=1,
                commit_seq=commit.commit_seq,
                change_kind="create",
                before_state=None,
                after_state=row,
                created_at=commit.created_at,
            )
        return row


__all__ = ["ArtifactIntegrityError", "ArtifactNotFoundError", "ArtifactRepository"]
