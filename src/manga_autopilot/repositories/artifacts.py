"""Work-local, immutable artifact publication and revisioned SQLite provenance.

A filesystem and SQLite cannot share one atomic transaction. Publish the
validated bytes first, then register metadata and entity history in one short
Work transaction. A failed registration may leave an orphan final file, but
never a committed row pointing at a file that was not published.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from PIL import Image, UnidentifiedImageError

from manga_autopilot.primitives import canonical_json, new_id
from manga_autopilot.repositories.work_lifecycle import WorkLifecycleRepository
from manga_autopilot.storage import (
    assert_managed_path,
    create_work_commit,
    create_work_entity_revision,
    repository_read,
    repository_write,
    validate_work_id,
)
from manga_autopilot.storage.paths import UnsafeStoragePathError
from manga_autopilot.storage.repository import assert_work_mutation_allowed

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


def _verify_published_file(
    target: Path,
    *,
    work_root: Path,
    expected_identity: tuple[int, int],
    expected_sha256: str,
    expected_size: int,
) -> None:
    """Validate the *published* pathname and pinned bytes inside the Work commit.

    The first SHA/size comes from the private temp file. Hard-link publication
    normally preserves its device/inode, but the visible final pathname can be
    replaced or edited before its READY row is committed. Read a pinned, regular,
    non-symlink final file and require that its inode and bytes still match the
    originally validated temp object. Inspect the pathname again after reading.

    This closes the deterministic publication-to-commit corruption window for
    repository-owned writes. An external actor that ignores Work/OS discipline
    can still change a local file *after* validation; a filesystem and SQLite
    cannot provide one atomic transaction. Read-time integrity checks remain.
    """
    try:
        assert_managed_path(
            target, containment_root=work_root,
            field_name="published Artifact final path",
        )
        before = target.lstat()
        identity = (before.st_dev, before.st_ino)
        if not stat.S_ISREG(before.st_mode) or identity != expected_identity:
            raise ArtifactIntegrityError(
                "published Artifact path changed before READY registration"
            )

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags), "rb") as published:
            opened = os.fstat(published.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != expected_identity
            ):
                raise ArtifactIntegrityError(
                    "published Artifact file identity changed before READY registration"
                )
            digest = hashlib.sha256()
            count = 0
            while piece := published.read(_READ_CHUNK_BYTES):
                digest.update(piece)
                count += len(piece)
            finished = os.fstat(published.fileno())

        after = target.lstat()
        assert_managed_path(
            target, containment_root=work_root,
            field_name="published Artifact final path",
        )
        if (
            not stat.S_ISREG(after.st_mode)
            or (after.st_dev, after.st_ino) != expected_identity
            or (finished.st_dev, finished.st_ino) != expected_identity
            or opened.st_size != finished.st_size
            or opened.st_mtime_ns != finished.st_mtime_ns
            or opened.st_ctime_ns != finished.st_ctime_ns
            or after.st_size != finished.st_size
            or after.st_mtime_ns != finished.st_mtime_ns
            or count != expected_size
            or digest.hexdigest() != expected_sha256
        ):
            raise ArtifactIntegrityError(
                "published Artifact bytes or identity changed before READY registration"
            )
    except (OSError, UnsafeStoragePathError) as exc:
        raise ArtifactIntegrityError(
            "published Artifact cannot be verified before READY registration"
        ) from exc


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
    source_payload: dict[str, Any],
) -> str:
    """Canonical source and render-options attestation in the Artifact commit."""
    return canonical_json({
        "attestation_version": 1,
        "artifact_id": artifact_id,
        "page_id": page_id,
        "dependency_fingerprint": fingerprint,
        "source_fingerprint_payload": source_payload,
    })


def verify_page_render_attestation(
    reason: str | None, artifact: dict[str, Any],
) -> bool:
    """Validate source digest and immutable Artifact/Work commit binding."""
    if not isinstance(reason, str):
        return False
    try:
        envelope = json.loads(reason)
        payload = envelope["source_fingerprint_payload"]
        if type(envelope["attestation_version"]) is not int:
            return False
        if envelope["attestation_version"] != 1 or not isinstance(payload, dict):
            return False
        if envelope["artifact_id"] != artifact["id"]:
            return False
        if envelope["page_id"] != artifact["scope_id"]:
            return False
        if payload["page_id"] != artifact["scope_id"]:
            return False
        if envelope["dependency_fingerprint"] != artifact["dependency_fingerprint"]:
            return False
        if payload["renderer"] != "legacy_page_renderer_v1":
            return False
        if not isinstance(payload["panel_artifacts"], list):
            return False
        return (
            canonical_json(envelope) == reason
            and hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
            == artifact["dependency_fingerprint"]
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


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
        source_fingerprint_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Export-only registration with an independently verified source proof.

        Generic register_local_file/bytes, including those with user-supplied
        commit guards, cannot issue this attestation. The Work commit, source
        attestation, READY Artifact and history record are atomic.
        """
        if not callable(commit_guard):
            raise ValueError("verified Page render requires a source commit guard")
        reason = verified_page_render_reason(
            artifact_id, page_id, dependency_fingerprint, source_fingerprint_payload,
        )
        if not verify_page_render_attestation(
            reason,
            {"id": artifact_id, "scope_id": page_id,
             "dependency_fingerprint": dependency_fingerprint},
        ):
            raise ValueError("verified Page render requires matching source digest")

        def attested_guard(conn: sqlite3.Connection) -> None:
            from manga_autopilot.services.page_application import (
                read_page_state_in_transaction,
            )
            from manga_autopilot.services.work_page_export import (
                PageExportConflictError,
            )

            current = read_page_state_in_transaction(conn, self.work_id, page_id)
            source_hash = hashlib.sha256(canonical_json({
                "page": current["page"], "layout": current["layout"],
                "slots": current["slots"], "panels": current["panels"],
            }).encode("utf-8")).hexdigest()
            active = [
                panel for panel in current["panels"]
                if panel["archived_at"] is None
            ]
            deps = source_fingerprint_payload["panel_artifacts"]
            if (
                current["page"]["archived_at"] is not None
                or not active
                or source_hash != source_fingerprint_payload["page_state_sha256"]
                or len(active) != len(deps)
                or {p["id"] for p in active} != {d["panel_id"] for d in deps}
            ):
                raise PageExportConflictError(
                    f"Page {page_id} changed before provenance attestation."
                )
            slots = {slot["id"]: slot for slot in current["slots"]}
            for dep in deps:
                panel = next(p for p in active if p["id"] == dep["panel_id"])
                slot = slots.get(dep["slot_id"])
                row = conn.execute(
                    """SELECT artifact_type, scope_type, scope_id, status,
                              archived_at, mime_type, sha256 FROM artifacts
                       WHERE id = ?""",
                    (dep["artifact_id"],),
                ).fetchone()
                if (
                    slot is None
                    or panel["revision"] != dep["panel_revision"]
                    or panel["layout_slot_id"] != dep["slot_id"]
                    or slot["revision"] != dep["slot_revision"]
                    or row is None
                    or row["artifact_type"] != "panel_candidate"
                    or row["scope_type"] != "panel"
                    or row["scope_id"] != panel["id"]
                    or row["status"] != "READY"
                    or row["archived_at"] is not None
                    or row["mime_type"] not in {
                        "image/png", "image/jpeg", "image/webp"
                    }
                    or row["sha256"] != dep["sha256"]
                    or (
                        panel["selected_candidate_id"] is not None
                        and panel["selected_candidate_id"] != dep["artifact_id"]
                    )
                ):
                    raise PageExportConflictError(
                        f"Page {page_id} candidate source failed attestation."
                    )
            # Keep #335's exact candidate uniqueness and commit ordering guard.
            commit_guard(conn)

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
                commit_guard=attested_guard,
                _verified_page_render=True,
                _verified_page_source_payload=source_fingerprint_payload,
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
        _verified_page_source_payload: dict[str, Any] | None = None,
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
        # Early refusal avoids avoidable immutable orphan files. The same
        # gate MUST run again under BEGIN IMMEDIATE at the Work commit, since
        # a lease can be acquired between this check and publication.
        with repository_read(work.database_path) as conn:
            assert_work_mutation_allowed(conn)
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
            validated = temp.stat()
            source_identity = (validated.st_dev, validated.st_ino)
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
            assert_work_mutation_allowed(conn)
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
                        _verified_page_source_payload,
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
            # The only READY state that may escape this transaction is one
            # whose actual published bytes and inode match the validated temp.
            # A failed check rolls back Commit, Artifact and EntityRevision.
            # Verify last, after guards and history writes, to minimize the
            # unavoidable local filesystem/SQLite coordination gap.
            _verify_published_file(
                target,
                work_root=work.root,
                expected_identity=source_identity,
                expected_sha256=digest,
                expected_size=size,
            )
        return row


__all__ = ["ArtifactIntegrityError", "ArtifactNotFoundError", "ArtifactRepository"]
