"""v2 Work lifecycle repository backed by Master/Work SQLite databases."""

from __future__ import annotations

import errno
import json
import os
import shutil
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from manga_autopilot.primitives import new_id, sha256_file
from manga_autopilot.storage import (
    WORK_MIGRATIONS,
    WORK_RECOVERY_QUARANTINE_DIR,
    WORK_STAGING_PREFIX,
    Migration,
    MigrationError,
    WorkPaths,
    assert_managed_path,
    assert_managed_regular_file,
    bootstrap_master_database,
    bootstrap_work_database,
    create_work_commit,
    create_work_entity_revision,
    ensure_storage_root,
    inspect_work_database,
    migrate_work_database,
    read_work_identity,
    repository_read,
    repository_write,
    storage_paths,
    validate_work_database,
    validate_work_id,
    verify_work_database_for_open,
    work_paths,
    write_connection,
)
from manga_autopilot.storage.work_manifest import (
    LEGACY_WORK_MANIFEST_FORMAT_VERSIONS,
    LIVE_MANIFEST_HASH_POLICY,
    LIVE_MANIFEST_INTEGRITY_MODE,
    WORK_MANIFEST_FORMAT,
    WORK_MANIFEST_FORMAT_VERSION,
    WorkManifestContractError,
    build_live_work_manifest,
    is_canonical_live_manifest,
    validate_manifest_common,
)


class WorkLifecycleError(RuntimeError):
    """Base class for Work lifecycle persistence failures."""


class WorkCreationError(WorkLifecycleError):
    """Raised when a Work cannot be created safely."""


class WorkNotFoundError(WorkLifecycleError):
    """Raised when a Work is not present in the Master catalog."""


class WorkManifestError(WorkLifecycleError):
    """Raised when Work manifest metadata is missing or inconsistent."""


class WorkIdentityMismatchError(WorkLifecycleError):
    """Raised when catalog, manifest, DB identity, and Work state disagree."""


class WorkUpgradeError(WorkLifecycleError):
    """Raised when a Work cannot be safely upgraded before opening."""


class WorkRecoveryError(WorkLifecycleError):
    """Raised when incomplete Work creation cannot be reconciled safely."""


@dataclass(frozen=True)
class WorkCatalogEntry:
    """Master-side discovery metadata for one Work."""

    work_id: str
    universe_id: str | None
    series_id: str | None
    title: str
    work_kind: str
    relative_work_path: str
    status: str
    manifest_hash: str | None
    created_at: str
    updated_at: str
    last_opened_at: str | None


@dataclass(frozen=True)
class PortableWorkInspection:
    """Portable Work inspection that does not require a Master database."""

    work_id: str
    root: Path
    database_path: Path
    manifest_path: Path
    manifest_format_version: int
    manifest_schema_version: int
    database_schema_version: int
    target_schema_version: int
    upgrade_required: bool
    manifest_refresh_required: bool


@dataclass(frozen=True)
class WorkRecoveryFinding:
    """One recoverable or diagnostic Work-creation filesystem state."""

    kind: str
    path: Path
    work_id: str | None
    valid: bool
    recommended_action: str | None
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class WorkHandle:
    """Opened Work state with Work DB authoritative metadata."""

    work_id: str
    root: Path
    database_path: Path
    manifest_path: Path
    title: str
    work_kind: str
    language: str
    reading_direction: str
    status: str
    current_commit_seq: int
    current_revision: int
    schema_version: int
    migration_backup_path: Path | None
    created_at: str
    updated_at: str
    catalog: WorkCatalogEntry


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_WORK_CREATION_LOCK_PREFIX = ".work-creation-lock-"
_QUARANTINE_RECEIPT_SUFFIX = ".recovery.json"
_QUARANTINE_PROTOCOL_VERSION = 1


class _WorkCreationLockActiveError(RuntimeError):
    """Raised when another process owns one Work creation/recovery lock."""


def _work_creation_lock_path(works_root: Path, work_id: str) -> Path:
    validate_work_id(work_id)
    return works_root / f"{_WORK_CREATION_LOCK_PREFIX}{work_id}.lock"


def _try_lock_handle(handle: Any) -> bool:
    """Acquire one cross-process advisory lock without blocking."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            busy_errnos = {
                errno.EACCES,
                errno.EAGAIN,
                getattr(errno, "EDEADLK", errno.EACCES),
            }
            if exc.errno in busy_errnos:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_handle(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _work_creation_lock(works_root: Path, work_id: str) -> Iterator[None]:
    """Own one Work lifecycle mutation across processes.

    Lock files are intentionally persistent. Deleting a lock filename after
    unlock can split ownership between a process holding the old inode and a
    process that creates/locks a replacement inode.
    """
    lock_path = _work_creation_lock_path(works_root, work_id)
    assert_managed_regular_file(
        lock_path,
        containment_root=works_root,
        field_name="Work creation lock",
        allow_missing=True,
    )
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        if not _try_lock_handle(handle):
            raise _WorkCreationLockActiveError(
                f"Work creation is active for {work_id!r}"
            )
        try:
            yield
        finally:
            _unlock_handle(handle)


def _work_creation_is_active(works_root: Path, work_id: str) -> bool:
    try:
        with _work_creation_lock(works_root, work_id):
            return False
    except _WorkCreationLockActiveError:
        return True


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    code
    for code in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if code is not None
)


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes where the platform supports it."""
    if os.name == "nt":
        # Python cannot portably open directory handles for fsync on Windows.
        # Atomic replace still applies there; Windows-specific durability is
        # covered separately rather than emulating unsafe handle semantics.
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise

    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        os.close(descriptor)


def _durable_replace_directory(source: Path, destination: Path) -> None:
    """Publish a prepared directory before later durable catalog registration."""
    _fsync_directory(source)
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _durable_move_directory(source: Path, destination: Path) -> None:
    """Move a directory durably when source and destination parents differ."""
    _fsync_directory(source)
    os.replace(source, destination)
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise WorkManifestError(f"refusing to replace symlinked JSON file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    if temp.is_symlink():
        raise WorkManifestError(f"refusing to use symlinked temp file: {temp}")
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def _read_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise WorkManifestError(f"Work manifest must not be a symlink: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkManifestError(f"Work manifest is missing: {path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkManifestError(f"Work manifest is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise WorkManifestError("Work manifest root must be a JSON object")
    return payload


def _catalog_entry(row: Any) -> WorkCatalogEntry:
    return WorkCatalogEntry(
        work_id=str(row["work_id"]),
        universe_id=row["universe_id"],
        series_id=row["series_id"],
        title=str(row["title"]),
        work_kind=str(row["work_kind"]),
        relative_work_path=str(row["relative_work_path"]),
        status=str(row["status"]),
        manifest_hash=row["manifest_hash"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_opened_at=row["last_opened_at"],
    )


def _target_schema_version(migrations: Iterable[Migration]) -> int:
    versions = [migration.version for migration in migrations]
    return max(versions, default=0)


def _master_lineage_fields(
    *,
    universe_source_id: str | None = None,
    series_source_id: str | None = None,
    source_checkpoint_id: str | None = None,
) -> tuple[str, ...]:
    """Return Master-lineage fields that require immutable Work snapshots."""
    values = (
        ("universe_source_id", universe_source_id),
        ("series_source_id", series_source_id),
        ("source_checkpoint_id", source_checkpoint_id),
    )
    return tuple(name for name, value in values if value is not None)


def _unsnapshotted_master_lineage_message(fields: Iterable[str]) -> str:
    field_list = ", ".join(fields)
    return (
        "Master-linked Work state requires immutable source snapshots before "
        "lineage can be persisted; Phase A supports standalone Work state only. "
        f"Unsnapshotted lineage fields: {field_list}"
    )


def _persisted_master_lineage_fields(
    database_path: Path,
    *,
    work_id: str,
) -> tuple[str, ...]:
    """Read persisted lineage when the Work metadata table already exists."""
    with repository_read(database_path) as connection:
        table = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'work_metadata'
            """
        ).fetchone()
        if table is None:
            return ()

        metadata = connection.execute(
            """
            SELECT universe_source_id, series_source_id, source_checkpoint_id
            FROM work_metadata
            WHERE work_id = ?
            """,
            (work_id,),
        ).fetchone()

    if metadata is None:
        return ()
    return _master_lineage_fields(
        universe_source_id=metadata["universe_source_id"],
        series_source_id=metadata["series_source_id"],
        source_checkpoint_id=metadata["source_checkpoint_id"],
    )


def _work_revision_state(
    *,
    work_id: str,
    universe_source_id: str | None,
    series_source_id: str | None,
    source_checkpoint_id: str | None,
    title: str,
    work_kind: str,
    language: str,
    reading_direction: str,
    status: str,
    current_revision: int,
    created_at: str,
    updated_at: str,
    completed_at: str | None,
) -> dict[str, Any]:
    """Return the canonical semantic Work state stored in revision history."""
    return {
        "schema_version": 1,
        "work_id": work_id,
        "universe_source_id": universe_source_id,
        "series_source_id": series_source_id,
        "source_checkpoint_id": source_checkpoint_id,
        "title": title,
        "work_kind": work_kind,
        "language": language,
        "reading_direction": reading_direction,
        "status": status,
        "current_revision": current_revision,
        "created_at": created_at,
        "updated_at": updated_at,
        "completed_at": completed_at,
    }


def _live_manifest(
    *,
    work_id: str,
    database_name: str,
    work_schema_version: int,
    created_at: str,
    app_version: str | None,
) -> dict[str, Any]:
    return build_live_work_manifest(
        work_id=work_id,
        database_name=database_name,
        work_schema_version=work_schema_version,
        created_at=created_at,
        app_version=app_version,
    )


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    expected_work_id: str | None,
    expected_database_name: str = "work.sqlite3",
) -> None:
    work_id = manifest.get("work_id")
    if (
        expected_work_id is not None
        and isinstance(work_id, str)
        and work_id != expected_work_id
    ):
        raise WorkIdentityMismatchError(
            f"manifest Work identity mismatch: expected {expected_work_id!r}, "
            f"got {work_id!r}"
        )

    try:
        validate_manifest_common(
            manifest,
            expected_work_id=expected_work_id,
            expected_database_name=expected_database_name,
        )
    except WorkManifestContractError as exc:
        if "manifest Work identity mismatch" in str(exc):
            raise WorkIdentityMismatchError(str(exc)) from exc
        raise WorkManifestError(str(exc)) from exc

    try:
        validate_work_id(str(manifest["work_id"]))
    except ValueError as exc:
        raise WorkManifestError(str(exc)) from exc


@contextmanager
def _recovery_validation_snapshot(root: Path) -> Iterator[Path]:
    """Copy recovery-critical SQLite inputs so scanning cannot mutate evidence."""
    manifest_path = root / "manifest.json"
    database_path = root / "work.sqlite3"

    assert_managed_regular_file(
        manifest_path,
        containment_root=root,
        field_name="Work manifest",
    )
    assert_managed_regular_file(
        database_path,
        containment_root=root,
        field_name="Work database",
    )

    with TemporaryDirectory(prefix="manga-autopilot-recovery-") as temp_dir:
        snapshot_root = Path(temp_dir) / root.name
        snapshot_root.mkdir()
        shutil.copy2(manifest_path, snapshot_root / manifest_path.name)
        shutil.copy2(database_path, snapshot_root / database_path.name)

        # Copy durable SQLite sidecars that may contain authoritative committed
        # state. The WAL index (-shm) is transient and is rebuilt in the
        # writable temporary snapshot rather than copied from recovery evidence.
        for suffix in ("-wal", "-journal"):
            source = database_path.with_name(database_path.name + suffix)
            if not source.exists() and not source.is_symlink():
                continue
            assert_managed_regular_file(
                source,
                containment_root=root,
                field_name=f"Work database sidecar {suffix}",
            )
            shutil.copy2(source, snapshot_root / source.name)

        yield snapshot_root


def inspect_work_directory(
    work_root: str | Path,
    *,
    migrations: Iterable[Migration] = WORK_MIGRATIONS,
    expected_work_id: str | None = None,
) -> PortableWorkInspection:
    """Inspect a portable Work directory without requiring Master DB access."""
    migration_set = tuple(migrations)
    root = Path(work_root).expanduser().resolve()
    manifest_path = root / "manifest.json"
    database_path = root / "work.sqlite3"

    assert_managed_regular_file(
        manifest_path,
        containment_root=root,
        field_name="Work manifest",
    )
    assert_managed_regular_file(
        database_path,
        containment_root=root,
        field_name="Work database",
    )

    manifest = _read_manifest(manifest_path)
    _validate_manifest(
        manifest,
        expected_work_id=expected_work_id,
        expected_database_name=database_path.name,
    )

    work_id = str(manifest["work_id"])
    database_inspection = inspect_work_database(
        database_path,
        migrations=migration_set,
    )
    database_schema_version = database_inspection.current_version

    identity = read_work_identity(database_path)
    if identity.work_id != work_id:
        raise WorkIdentityMismatchError(
            f"Work DB identity mismatch: manifest={work_id!r}, "
            f"database={identity.work_id!r}"
        )
    manifest_schema_version = int(manifest["work_schema_version"])
    if manifest_schema_version > database_schema_version:
        raise WorkManifestError(
            f"manifest schema version is ahead of Work DB for {work_id!r}: "
            f"manifest={manifest_schema_version}, database={database_schema_version}"
        )

    target_schema_version = _target_schema_version(migration_set)
    manifest_refresh_required = (
        manifest_schema_version != database_schema_version
        or not is_canonical_live_manifest(manifest)
    )

    return PortableWorkInspection(
        work_id=work_id,
        root=root,
        database_path=database_path,
        manifest_path=manifest_path,
        manifest_format_version=int(manifest["format_version"]),
        manifest_schema_version=manifest_schema_version,
        database_schema_version=database_schema_version,
        target_schema_version=target_schema_version,
        upgrade_required=database_schema_version < target_schema_version,
        manifest_refresh_required=manifest_refresh_required,
    )


class WorkLifecycleRepository:
    """Create, open, and discover v2 Works.

    Master is discovery metadata. Work-local semantic state remains authoritative
    in work.sqlite3. Opening a Work first upgrades its DB safely to the configured
    Work schema before a writable handle is returned.
    """

    def __init__(
        self,
        storage_root: str | Path,
        *,
        app_version: str | None = None,
        work_migrations: Iterable[Migration] = WORK_MIGRATIONS,
    ) -> None:
        self.storage_root = ensure_storage_root(storage_root)
        self.paths = storage_paths(self.storage_root)
        assert_managed_regular_file(
            self.paths.master_db,
            containment_root=self.storage_root,
            field_name="Master database",
            allow_missing=True,
        )
        self.app_version = app_version
        self.work_migrations = tuple(work_migrations)
        bootstrap_master_database(
            self.paths.master_db,
            app_version=app_version,
        )

    def create_work(
        self,
        *,
        title: str,
        work_id: str | None = None,
        work_kind: str = "standalone",
        language: str = "ja",
        reading_direction: str = "RTL_TOP_TO_BOTTOM",
        status: str = "DRAFT",
        universe_id: str | None = None,
        series_id: str | None = None,
    ) -> WorkHandle:
        """Create a self-contained standalone Work and register it in Master.

        Master-linked lineage is rejected until immutable source snapshots can
        be persisted inside the Work.
        """
        for field_name, value in (
            ("title", title),
            ("work_kind", work_kind),
            ("language", language),
            ("reading_direction", reading_direction),
            ("status", status),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must be non-empty")

        lineage_fields = _master_lineage_fields(
            universe_source_id=universe_id,
            series_source_id=series_id,
        )
        if lineage_fields:
            raise ValueError(
                _unsnapshotted_master_lineage_message(lineage_fields)
            )

        resolved_work_id = work_id or new_id("work")
        final_paths = work_paths(self.storage_root, resolved_work_id)
        relative_work_path = final_paths.root.relative_to(self.storage_root).as_posix()
        staging_root = self.paths.works / f"{WORK_STAGING_PREFIX}{resolved_work_id}"
        staging_paths = WorkPaths(work_id=resolved_work_id, root=staging_root)
        created_at = _utc_now_iso()

        try:
            with _work_creation_lock(self.paths.works, resolved_work_id):
                staging_created = False
                try:
                    if final_paths.root.exists():
                        raise WorkCreationError(
                            f"Work directory already exists: {final_paths.root}"
                        )
                    if staging_root.exists() or staging_root.is_symlink():
                        raise WorkCreationError(
                            "staging directory already exists for Work "
                            f"{resolved_work_id!r}: {staging_root}"
                        )

                    with repository_read(self.paths.master_db) as connection:
                        existing = connection.execute(
                            "SELECT 1 FROM work_catalog WHERE work_id = ?",
                            (resolved_work_id,),
                        ).fetchone()
                    if existing is not None:
                        raise WorkCreationError(
                            "Work already exists in Master catalog: "
                            f"{resolved_work_id}"
                        )

                    assert_managed_path(
                        staging_paths.root,
                        containment_root=self.paths.works,
                        field_name="Work staging directory",
                    )
                    staging_paths.root.mkdir(parents=True, exist_ok=False)
                    staging_created = True
                    staging_paths.assets.mkdir()
                    staging_paths.cache.mkdir()
                    staging_paths.exports.mkdir()

                    bootstrap_work_database(
                        staging_paths.work_db,
                        work_id=resolved_work_id,
                        app_version=self.app_version,
                        migrations=self.work_migrations,
                    )

                    with repository_write(staging_paths.work_db) as connection:
                        commit = create_work_commit(
                            connection,
                            commit_id=new_id("commit"),
                            actor_type="system",
                            operation_type="create_work",
                            reason="create Work",
                            created_at=created_at,
                        )
                        connection.execute(
                            """
                            INSERT INTO work_metadata (
                                work_id,
                                universe_source_id,
                                series_source_id,
                                title,
                                work_kind,
                                language,
                                reading_direction,
                                status,
                                current_commit_seq,
                                current_revision,
                                created_at,
                                updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                resolved_work_id,
                                universe_id,
                                series_id,
                                title,
                                work_kind,
                                language,
                                reading_direction,
                                status,
                                commit.commit_seq,
                                1,
                                created_at,
                                created_at,
                            ),
                        )
                        create_work_entity_revision(
                            connection,
                            revision_id=new_id("revision"),
                            entity_type="work",
                            entity_id=resolved_work_id,
                            entity_revision=1,
                            commit_seq=commit.commit_seq,
                            change_kind="create",
                            after_state=_work_revision_state(
                                work_id=resolved_work_id,
                                universe_source_id=universe_id,
                                series_source_id=series_id,
                                source_checkpoint_id=None,
                                title=title,
                                work_kind=work_kind,
                                language=language,
                                reading_direction=reading_direction,
                                status=status,
                                current_revision=1,
                                created_at=created_at,
                                updated_at=created_at,
                                completed_at=None,
                            ),
                            created_at=created_at,
                        )

                    with write_connection(staging_paths.work_db) as connection:
                        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                    manifest = _live_manifest(
                        work_id=resolved_work_id,
                        database_name=staging_paths.work_db.name,
                        work_schema_version=_target_schema_version(
                            self.work_migrations
                        ),
                        created_at=created_at,
                        app_version=self.app_version,
                    )
                    _write_json_atomic(staging_paths.manifest_json, manifest)
                    manifest_hash = sha256_file(staging_paths.manifest_json)

                    # Durability ordering is intentional:
                    # 1. Work DB/manifest content is flushed,
                    # 2. staging directory entries are fsynced where supported,
                    # 3. staging is atomically renamed to its final path,
                    # 4. the Works parent directory is fsynced,
                    # 5. only then may Master durably reference the final Work.
                    _durable_replace_directory(
                        staging_paths.root,
                        final_paths.root,
                    )
                    staging_created = False

                    with repository_write(self.paths.master_db) as connection:
                        connection.execute(
                            """
                            INSERT INTO work_catalog (
                                work_id,
                                universe_id,
                                series_id,
                                title,
                                work_kind,
                                relative_work_path,
                                status,
                                manifest_hash,
                                created_at,
                                updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                resolved_work_id,
                                universe_id,
                                series_id,
                                title,
                                work_kind,
                                relative_work_path,
                                status,
                                manifest_hash,
                                created_at,
                                created_at,
                            ),
                        )
                except Exception:
                    if staging_created and staging_root.exists():
                        shutil.rmtree(staging_root, ignore_errors=True)
                    raise
        except _WorkCreationLockActiveError as exc:
            raise WorkCreationError(
                f"Work creation is already active for {resolved_work_id!r}"
            ) from exc
        except Exception as exc:
            if isinstance(exc, WorkLifecycleError):
                raise
            raise WorkCreationError(
                f"failed to create Work {resolved_work_id!r}: {exc}"
            ) from exc

        return self.open_work(resolved_work_id)

    def open_work(self, work_id: str) -> WorkHandle:
        """Safely upgrade, validate, and open one cataloged Work."""
        with repository_read(self.paths.master_db) as connection:
            row = connection.execute(
                "SELECT * FROM work_catalog WHERE work_id = ?",
                (work_id,),
            ).fetchone()
        if row is None:
            raise WorkNotFoundError(f"Work is not in Master catalog: {work_id}")

        catalog = _catalog_entry(row)
        catalog_lineage_fields = _master_lineage_fields(
            universe_source_id=catalog.universe_id,
            series_source_id=catalog.series_id,
        )
        if catalog_lineage_fields:
            raise WorkIdentityMismatchError(
                _unsnapshotted_master_lineage_message(catalog_lineage_fields)
            )

        root = self._resolve_catalog_path(catalog.relative_work_path)
        try:
            inspection = inspect_work_directory(
                root,
                migrations=self.work_migrations,
                expected_work_id=work_id,
            )
        except MigrationError as exc:
            raise WorkUpgradeError(
                f"failed to upgrade Work {work_id!r} before open: {exc}"
            ) from exc
        if inspection.work_id != work_id:
            raise WorkIdentityMismatchError(
                f"portable Work identity mismatch: catalog={work_id!r}, "
                f"work={inspection.work_id!r}"
            )

        persisted_lineage_fields = _persisted_master_lineage_fields(
            inspection.database_path,
            work_id=work_id,
        )
        if persisted_lineage_fields:
            raise WorkIdentityMismatchError(
                _unsnapshotted_master_lineage_message(persisted_lineage_fields)
            )

        manifest = _read_manifest(inspection.manifest_path)

        try:
            if inspection.upgrade_required:
                migration = migrate_work_database(
                    inspection.database_path,
                    migrations=self.work_migrations,
                    app_version=self.app_version,
                )
            else:
                migration = verify_work_database_for_open(
                    inspection.database_path,
                    migrations=self.work_migrations,
                    work_id=work_id,
                )
        except MigrationError as exc:
            raise WorkUpgradeError(
                f"failed to upgrade Work {work_id!r} before open: {exc}"
            ) from exc

        schema_version = migration.current_version
        created_at = str(manifest.get("created_at") or _utc_now_iso())
        normalized_manifest = _live_manifest(
            work_id=work_id,
            database_name=inspection.database_path.name,
            work_schema_version=schema_version,
            created_at=created_at,
            app_version=self.app_version or manifest.get("app_version"),
        )

        if manifest != normalized_manifest:
            try:
                _write_json_atomic(inspection.manifest_path, normalized_manifest)
            except OSError as exc:
                raise WorkManifestError(
                    f"Work DB is valid but live manifest refresh failed for "
                    f"{work_id!r}: {exc}"
                ) from exc

        refreshed_inspection = inspect_work_directory(
            root,
            migrations=self.work_migrations,
        )
        if refreshed_inspection.upgrade_required:
            raise WorkUpgradeError(
                f"Work {work_id!r} still requires migration after upgrade"
            )
        if refreshed_inspection.manifest_refresh_required:
            raise WorkManifestError(
                f"Work {work_id!r} live manifest is not synchronized after upgrade"
            )

        identity = read_work_identity(inspection.database_path)
        if identity.work_id != work_id:
            raise WorkIdentityMismatchError(
                f"Work DB identity mismatch: catalog={work_id!r}, "
                f"database={identity.work_id!r}"
            )

        with repository_read(inspection.database_path) as connection:
            metadata = connection.execute(
                "SELECT * FROM work_metadata WHERE work_id = ?",
                (work_id,),
            ).fetchone()

        if metadata is None:
            raise WorkIdentityMismatchError(
                f"Work DB has no authoritative work_metadata row for {work_id!r}"
            )

        metadata_lineage_fields = _master_lineage_fields(
            universe_source_id=metadata["universe_source_id"],
            series_source_id=metadata["series_source_id"],
            source_checkpoint_id=metadata["source_checkpoint_id"],
        )
        if metadata_lineage_fields:
            raise WorkIdentityMismatchError(
                _unsnapshotted_master_lineage_message(metadata_lineage_fields)
            )

        opened_at = _utc_now_iso()
        manifest_hash = sha256_file(inspection.manifest_path)
        with repository_write(self.paths.master_db) as connection:
            connection.execute(
                """
                UPDATE work_catalog
                SET manifest_hash = ?,
                    last_opened_at = ?
                WHERE work_id = ?
                """,
                (manifest_hash, opened_at, work_id),
            )

        refreshed_catalog = WorkCatalogEntry(
            **{
                **catalog.__dict__,
                "manifest_hash": manifest_hash,
                "last_opened_at": opened_at,
            }
        )
        return WorkHandle(
            work_id=work_id,
            root=root,
            database_path=inspection.database_path,
            manifest_path=inspection.manifest_path,
            title=str(metadata["title"]),
            work_kind=str(metadata["work_kind"]),
            language=str(metadata["language"]),
            reading_direction=str(metadata["reading_direction"]),
            status=str(metadata["status"]),
            current_commit_seq=int(metadata["current_commit_seq"]),
            current_revision=int(metadata["current_revision"]),
            schema_version=schema_version,
            migration_backup_path=migration.backup_path,
            created_at=str(metadata["created_at"]),
            updated_at=str(metadata["updated_at"]),
            catalog=refreshed_catalog,
        )

    def inspect_quarantine(self) -> tuple[WorkRecoveryFinding, ...]:
        """Inspect completed and incomplete quarantine states without mutation."""
        quarantine_root = self.paths.works / WORK_RECOVERY_QUARANTINE_DIR
        if not quarantine_root.exists() and not quarantine_root.is_symlink():
            return ()

        try:
            assert_managed_path(
                quarantine_root,
                containment_root=self.paths.works,
                field_name="recovery quarantine",
            )
        except ValueError as exc:
            return (
                WorkRecoveryFinding(
                    kind="QUARANTINE_INVALID",
                    path=quarantine_root,
                    work_id=None,
                    valid=False,
                    recommended_action=None,
                    diagnostics=(str(exc),),
                ),
            )
        if quarantine_root.is_symlink() or not quarantine_root.is_dir():
            return (
                WorkRecoveryFinding(
                    kind="QUARANTINE_INVALID",
                    path=quarantine_root,
                    work_id=None,
                    valid=False,
                    recommended_action=None,
                    diagnostics=(
                        "recovery quarantine must be a real directory: "
                        f"{quarantine_root}",
                    ),
                ),
            )

        findings: list[WorkRecoveryFinding] = []
        receipt_destinations: set[Path] = set()
        for receipt in sorted(
            quarantine_root.glob(f"*{_QUARANTINE_RECEIPT_SUFFIX}"),
            key=lambda item: item.name,
        ):
            destination = receipt.with_name(
                receipt.name[: -len(_QUARANTINE_RECEIPT_SUFFIX)]
            )
            receipt_destinations.add(destination)
            try:
                assert_managed_regular_file(
                    receipt,
                    containment_root=quarantine_root,
                    field_name="quarantine receipt",
                )
                payload = json.loads(receipt.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("quarantine receipt root must be an object")
                raw_work_id = payload.get("work_id")
                if not isinstance(raw_work_id, str):
                    raise ValueError("quarantine receipt work_id must be a string")
                work_id = validate_work_id(raw_work_id)
                state = payload.get("state", "complete")
                if state not in {"prepared", "complete"}:
                    raise ValueError(
                        f"unsupported quarantine receipt state: {state!r}"
                    )
                declared_destination = payload.get("destination")
                if (
                    declared_destination is not None
                    and declared_destination != destination.name
                ):
                    raise ValueError(
                        "quarantine receipt destination does not match filename"
                    )
            except Exception as exc:
                findings.append(
                    WorkRecoveryFinding(
                        kind="QUARANTINE_INVALID",
                        path=receipt,
                        work_id=None,
                        valid=False,
                        recommended_action=None,
                        diagnostics=(f"{type(exc).__name__}: {exc}",),
                    )
                )
                continue

            staging = self.paths.works / f"{WORK_STAGING_PREFIX}{work_id}"
            destination_exists = (
                destination.exists() and not destination.is_symlink()
            )
            staging_exists = staging.exists() or staging.is_symlink()

            if state == "complete" and destination_exists:
                findings.append(
                    WorkRecoveryFinding(
                        kind="QUARANTINE_COMPLETE",
                        path=destination,
                        work_id=work_id,
                        valid=True,
                        recommended_action=None,
                        diagnostics=(),
                    )
                )
                continue

            if state == "prepared" and (
                (staging_exists and not destination_exists)
                or (destination_exists and not staging_exists)
            ):
                findings.append(
                    WorkRecoveryFinding(
                        kind="QUARANTINE_INCOMPLETE",
                        path=receipt,
                        work_id=work_id,
                        valid=False,
                        recommended_action="quarantine",
                        diagnostics=(
                            "quarantine protocol is prepared but not complete",
                        ),
                    )
                )
                continue

            findings.append(
                WorkRecoveryFinding(
                    kind="QUARANTINE_INVALID",
                    path=destination if destination_exists else receipt,
                    work_id=work_id,
                    valid=False,
                    recommended_action=None,
                    diagnostics=(
                        "quarantine receipt/filesystem state is inconsistent",
                    ),
                )
            )

        for destination in sorted(
            (
                item
                for item in quarantine_root.iterdir()
                if item.is_dir() and item not in receipt_destinations
            ),
            key=lambda item: item.name,
        ):
            marker = f"-{WORK_STAGING_PREFIX[1:]}"
            _prefix, separator, raw_work_id = destination.name.partition(marker)
            work_id: str | None = None
            if separator and raw_work_id:
                try:
                    work_id = validate_work_id(raw_work_id)
                except ValueError:
                    work_id = None
            findings.append(
                WorkRecoveryFinding(
                    kind="QUARANTINE_INCOMPLETE",
                    path=destination,
                    work_id=work_id,
                    valid=False,
                    recommended_action="quarantine" if work_id else None,
                    diagnostics=(
                        "quarantined evidence directory has no recovery receipt",
                    ),
                )
            )

        return tuple(findings)

    def scan_recovery(self) -> tuple[WorkRecoveryFinding, ...]:
        """Detect incomplete Work/catalog states without mutating either side."""
        with repository_read(self.paths.master_db) as connection:
            catalog_rows = connection.execute(
                "SELECT * FROM work_catalog"
            ).fetchall()

        catalog_entries = tuple(_catalog_entry(row) for row in catalog_rows)
        cataloged_ids = {entry.work_id for entry in catalog_entries}

        findings: list[WorkRecoveryFinding] = [
            finding
            for finding in self.inspect_quarantine()
            if finding.kind != "QUARANTINE_COMPLETE"
        ]
        for catalog in sorted(catalog_entries, key=lambda entry: entry.work_id):
            relative = Path(catalog.relative_work_path)
            if relative.is_absolute() or ".." in relative.parts:
                findings.append(
                    WorkRecoveryFinding(
                        kind="CATALOG_WORK_PATH_INVALID",
                        path=self.storage_root,
                        work_id=catalog.work_id,
                        valid=False,
                        recommended_action=None,
                        diagnostics=(
                            f"unsafe catalog work path: "
                            f"{catalog.relative_work_path!r}",
                        ),
                    )
                )
                continue

            catalog_path = self.storage_root / relative
            if catalog_path.is_symlink():
                findings.append(
                    WorkRecoveryFinding(
                        kind="CATALOG_WORK_PATH_INVALID",
                        path=catalog_path,
                        work_id=catalog.work_id,
                        valid=False,
                        recommended_action=None,
                        diagnostics=(
                            f"catalog Work target must not be a symlink: "
                            f"{catalog_path}",
                        ),
                    )
                )
                continue

            try:
                root = self._resolve_catalog_path(catalog.relative_work_path)
            except WorkIdentityMismatchError as exc:
                findings.append(
                    WorkRecoveryFinding(
                        kind="CATALOG_WORK_PATH_INVALID",
                        path=self.storage_root,
                        work_id=catalog.work_id,
                        valid=False,
                        recommended_action=None,
                        diagnostics=(str(exc),),
                    )
                )
                continue

            if not root.exists() and not root.is_symlink():
                findings.append(
                    WorkRecoveryFinding(
                        kind="CATALOG_WORK_MISSING",
                        path=root,
                        work_id=catalog.work_id,
                        valid=False,
                        recommended_action=None,
                        diagnostics=(
                            f"catalog points to missing Work directory: {root}",
                        ),
                    )
                )
                continue

            if root.is_symlink() or not root.is_dir():
                findings.append(
                    WorkRecoveryFinding(
                        kind="CATALOG_WORK_PATH_INVALID",
                        path=root,
                        work_id=catalog.work_id,
                        valid=False,
                        recommended_action=None,
                        diagnostics=(
                            "catalog Work target must be a real directory: "
                            f"{root}",
                        ),
                    )
                )
                continue

            invalid_critical = tuple(
                candidate
                for candidate in (root / "work.sqlite3", root / "manifest.json")
                if (
                    not candidate.exists()
                    or candidate.is_symlink()
                    or not candidate.is_file()
                )
            )
            if invalid_critical:
                findings.append(
                    WorkRecoveryFinding(
                        kind="CATALOG_WORK_INCOMPLETE",
                        path=root,
                        work_id=catalog.work_id,
                        valid=False,
                        recommended_action=None,
                        diagnostics=tuple(
                            "cataloged Work critical file is missing or invalid: "
                            f"{candidate}"
                            for candidate in invalid_critical
                        ),
                    )
                )

        for entry in sorted(self.paths.works.iterdir(), key=lambda path: path.name):
            if entry.name == WORK_RECOVERY_QUARANTINE_DIR:
                continue

            if entry.name.startswith(WORK_STAGING_PREFIX):
                work_id = entry.name[len(WORK_STAGING_PREFIX) :] or None
                creation_active = False
                if work_id is not None:
                    try:
                        creation_active = _work_creation_is_active(
                            self.paths.works,
                            work_id,
                        )
                    except ValueError:
                        creation_active = False
                if creation_active:
                    findings.append(
                        WorkRecoveryFinding(
                            kind="ACTIVE_STAGING",
                            path=entry,
                            work_id=work_id,
                            valid=False,
                            recommended_action=None,
                            diagnostics=(
                                "Work staging is owned by an active creator; "
                                "recovery mutation is not allowed",
                            ),
                        )
                    )
                    continue

                valid, diagnostics = self._validate_recovery_directory(
                    entry,
                    expected_work_id=work_id,
                )
                findings.append(
                    WorkRecoveryFinding(
                        kind=(
                            "STALE_STAGING_VALID"
                            if valid
                            else "STALE_STAGING_INVALID"
                        ),
                        path=entry,
                        work_id=work_id,
                        valid=valid,
                        recommended_action="finalize" if valid else "quarantine",
                        diagnostics=diagnostics,
                    )
                )
                continue

            if entry.name.startswith("."):
                continue
            if not entry.is_dir() and not entry.is_symlink():
                continue
            if entry.name in cataloged_ids:
                continue

            try:
                creation_active = _work_creation_is_active(
                    self.paths.works,
                    entry.name,
                )
            except ValueError:
                creation_active = False
            if creation_active:
                findings.append(
                    WorkRecoveryFinding(
                        kind="ACTIVE_WORK_CREATION",
                        path=entry,
                        work_id=entry.name,
                        valid=False,
                        recommended_action=None,
                        diagnostics=(
                            "final Work path is still owned by an active creator; "
                            "catalog recovery is not allowed",
                        ),
                    )
                )
                continue

            valid, diagnostics = self._validate_recovery_directory(
                entry,
                expected_work_id=entry.name,
            )
            findings.append(
                WorkRecoveryFinding(
                    kind=(
                        "UNREGISTERED_WORK_VALID"
                        if valid
                        else "UNREGISTERED_WORK_INVALID"
                    ),
                    path=entry,
                    work_id=entry.name,
                    valid=valid,
                    recommended_action="register" if valid else None,
                    diagnostics=diagnostics,
                )
            )

        return tuple(findings)

    def reconcile_orphan_work(self, work_id: str) -> WorkCatalogEntry:
        """Register a valid finalized Work that is missing from Master catalog."""
        try:
            with _work_creation_lock(self.paths.works, work_id):
                return self._reconcile_orphan_work_owned(work_id)
        except _WorkCreationLockActiveError as exc:
            raise WorkRecoveryError(
                f"Work creation is still active for {work_id!r}"
            ) from exc

    def _reconcile_orphan_work_owned(self, work_id: str) -> WorkCatalogEntry:
        """Register an orphan while the caller owns the Work lifecycle lock."""
        final_paths = work_paths(self.storage_root, work_id)
        relative_work_path = final_paths.root.relative_to(self.storage_root).as_posix()

        with repository_read(self.paths.master_db) as connection:
            existing = connection.execute(
                "SELECT * FROM work_catalog WHERE work_id = ?",
                (work_id,),
            ).fetchone()
        if existing is not None:
            entry = _catalog_entry(existing)
            if entry.relative_work_path != relative_work_path:
                raise WorkRecoveryError(
                    f"catalog path mismatch for existing Work {work_id!r}: "
                    f"{entry.relative_work_path!r}"
                )
            return entry

        valid, diagnostics = self._validate_recovery_directory(
            final_paths.root,
            expected_work_id=work_id,
        )
        if not valid:
            raise WorkRecoveryError(
                f"cannot register invalid orphan Work {work_id!r}: "
                + "; ".join(diagnostics)
            )

        with repository_read(final_paths.work_db) as connection:
            metadata = connection.execute(
                "SELECT * FROM work_metadata WHERE work_id = ?",
                (work_id,),
            ).fetchone()
        if metadata is None:
            raise WorkRecoveryError(
                f"orphan Work {work_id!r} has no authoritative work_metadata row"
            )

        manifest_hash = sha256_file(final_paths.manifest_json)
        with repository_write(self.paths.master_db) as connection:
            connection.execute(
                """
                INSERT INTO work_catalog (
                    work_id,
                    universe_id,
                    series_id,
                    title,
                    work_kind,
                    relative_work_path,
                    status,
                    manifest_hash,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work_id,
                    metadata["universe_source_id"],
                    metadata["series_source_id"],
                    metadata["title"],
                    metadata["work_kind"],
                    relative_work_path,
                    metadata["status"],
                    manifest_hash,
                    metadata["created_at"],
                    metadata["updated_at"],
                ),
            )

        with repository_read(self.paths.master_db) as connection:
            row = connection.execute(
                "SELECT * FROM work_catalog WHERE work_id = ?",
                (work_id,),
            ).fetchone()
        if row is None:
            raise WorkRecoveryError(
                f"catalog registration did not persist for Work {work_id!r}"
            )
        return _catalog_entry(row)

    def finalize_staging_work(self, work_id: str) -> WorkCatalogEntry:
        """Finalize a complete stale staging Work and register it idempotently."""
        final_paths = work_paths(self.storage_root, work_id)
        staging = self.paths.works / f"{WORK_STAGING_PREFIX}{work_id}"

        try:
            with _work_creation_lock(self.paths.works, work_id):
                if not staging.exists() and not staging.is_symlink():
                    if final_paths.root.exists() and not final_paths.root.is_symlink():
                        return self._reconcile_orphan_work_owned(work_id)
                    raise WorkRecoveryError(
                        f"staging Work does not exist for {work_id!r}: {staging}"
                    )

                valid, diagnostics = self._validate_recovery_directory(
                    staging,
                    expected_work_id=work_id,
                )
                if not valid:
                    raise WorkRecoveryError(
                        f"cannot finalize invalid staging Work {work_id!r}: "
                        + "; ".join(diagnostics)
                    )
                if final_paths.root.exists() or final_paths.root.is_symlink():
                    raise WorkRecoveryError(
                        f"final Work path already exists for {work_id!r}: "
                        f"{final_paths.root}"
                    )

                _durable_replace_directory(staging, final_paths.root)
                # If catalog registration fails, leave the finalized Work intact
                # so a later scan can reconcile it without regenerating identity.
                return self._reconcile_orphan_work_owned(work_id)
        except _WorkCreationLockActiveError as exc:
            raise WorkRecoveryError(
                f"Work creation is still active for {work_id!r}"
            ) from exc

    def quarantine_staging_work(
        self,
        work_id: str,
        *,
        reason: str | None = None,
    ) -> Path:
        """Move stale staging into a durable, retryable quarantine protocol."""
        work_paths(self.storage_root, work_id)  # validates one safe component
        staging = self.paths.works / f"{WORK_STAGING_PREFIX}{work_id}"

        try:
            with _work_creation_lock(self.paths.works, work_id):
                quarantine_root = (
                    self.paths.works / WORK_RECOVERY_QUARANTINE_DIR
                )
                try:
                    assert_managed_path(
                        quarantine_root,
                        containment_root=self.paths.works,
                        field_name="recovery quarantine",
                    )
                except ValueError as exc:
                    raise WorkRecoveryError(str(exc)) from exc
                if quarantine_root.is_symlink():
                    raise WorkRecoveryError(
                        "recovery quarantine must not be a symlink: "
                        f"{quarantine_root}"
                    )
                quarantine_created = not quarantine_root.exists()
                quarantine_root.mkdir(exist_ok=True)
                if quarantine_created:
                    _fsync_directory(self.paths.works)

                prepared: tuple[Path, Path, dict[str, Any]] | None = None
                completed: list[Path] = []
                orphaned: list[Path] = []
                for finding in self.inspect_quarantine():
                    if finding.work_id != work_id:
                        continue
                    if finding.kind == "QUARANTINE_COMPLETE":
                        completed.append(finding.path)
                    elif finding.kind == "QUARANTINE_INCOMPLETE":
                        if finding.path.suffix == ".json":
                            receipt = finding.path
                            destination = receipt.with_name(
                                receipt.name[: -len(_QUARANTINE_RECEIPT_SUFFIX)]
                            )
                            try:
                                payload = json.loads(
                                    receipt.read_text(encoding="utf-8")
                                )
                            except (OSError, json.JSONDecodeError):
                                continue
                            if isinstance(payload, dict):
                                prepared = (destination, receipt, payload)
                        elif finding.path.is_dir():
                            orphaned.append(finding.path)

                staging_exists = staging.exists() or staging.is_symlink()
                if not staging_exists:
                    if prepared is not None:
                        destination, receipt, payload = prepared
                        if not destination.is_dir() or destination.is_symlink():
                            raise WorkRecoveryError(
                                "prepared quarantine destination is missing or "
                                f"invalid: {destination}"
                            )
                        _fsync_directory(self.paths.works)
                        _fsync_directory(quarantine_root)
                        completed_payload = {
                            **payload,
                            "state": "complete",
                            "completed_at": _utc_now_iso(),
                            "quarantined_at": payload.get(
                                "quarantined_at",
                                _utc_now_iso(),
                            ),
                        }
                        _write_json_atomic(receipt, completed_payload)
                        return destination
                    if orphaned:
                        destination = sorted(orphaned, key=lambda p: p.name)[-1]
                        receipt = destination.with_name(
                            destination.name + _QUARANTINE_RECEIPT_SUFFIX
                        )
                        completed_at = _utc_now_iso()
                        _fsync_directory(self.paths.works)
                        _fsync_directory(quarantine_root)
                        _write_json_atomic(
                            receipt,
                            {
                                "protocol_version": _QUARANTINE_PROTOCOL_VERSION,
                                "state": "complete",
                                "work_id": work_id,
                                "source": staging.name,
                                "destination": destination.name,
                                "prepared_at": None,
                                "completed_at": completed_at,
                                "quarantined_at": completed_at,
                                "reason": reason,
                                "recovered_receipt": True,
                            },
                        )
                        return destination
                    if completed:
                        return sorted(completed, key=lambda p: p.name)[-1]
                    raise WorkRecoveryError(
                        f"staging Work does not exist for {work_id!r}: {staging}"
                    )

                if prepared is None:
                    stamp = datetime.now(timezone.utc).strftime(
                        "%Y%m%dT%H%M%S%fZ"
                    )
                    destination = quarantine_root / (
                        f"{stamp}-{WORK_STAGING_PREFIX[1:]}{work_id}"
                    )
                    receipt = destination.with_name(
                        destination.name + _QUARANTINE_RECEIPT_SUFFIX
                    )
                    try:
                        assert_managed_path(
                            destination,
                            containment_root=quarantine_root,
                            field_name="recovery quarantine destination",
                        )
                    except ValueError as exc:
                        raise WorkRecoveryError(str(exc)) from exc
                    prepared_at = _utc_now_iso()
                    payload = {
                        "protocol_version": _QUARANTINE_PROTOCOL_VERSION,
                        "state": "prepared",
                        "work_id": work_id,
                        "source": staging.name,
                        "destination": destination.name,
                        "prepared_at": prepared_at,
                        "completed_at": None,
                        "quarantined_at": None,
                        "reason": reason,
                    }
                    _write_json_atomic(receipt, payload)
                else:
                    destination, receipt, payload = prepared
                    if destination.exists() or destination.is_symlink():
                        raise WorkRecoveryError(
                            "prepared quarantine destination already exists "
                            "while staging is also present"
                        )

                _durable_move_directory(staging, destination)

                completed_at = _utc_now_iso()
                _write_json_atomic(
                    receipt,
                    {
                        **payload,
                        "state": "complete",
                        "completed_at": completed_at,
                        "quarantined_at": completed_at,
                    },
                )
                return destination
        except _WorkCreationLockActiveError as exc:
            raise WorkRecoveryError(
                f"Work creation is still active for {work_id!r}"
            ) from exc

    def _validate_recovery_directory(
        self,
        root: Path,
        *,
        expected_work_id: str | None,
    ) -> tuple[bool, tuple[str, ...]]:
        diagnostics: list[str] = []
        snapshot_root: Path | None = None
        try:
            assert_managed_path(
                root,
                containment_root=self.paths.works,
                field_name="recovery Work root",
            )
            if root.is_symlink():
                return False, (f"symlink recovery entry is not trusted: {root}",)
            if not root.is_dir():
                return False, (f"recovery entry is not a directory: {root}",)

            with _recovery_validation_snapshot(root) as snapshot_root:
                inspection = inspect_work_directory(
                    snapshot_root,
                    migrations=self.work_migrations,
                    expected_work_id=expected_work_id,
                )
                validate_work_database(
                    inspection.database_path,
                    migrations=self.work_migrations,
                    work_id=inspection.work_id,
                )
                with repository_read(inspection.database_path) as connection:
                    metadata = connection.execute(
                        """
                        SELECT
                            work_id,
                            universe_source_id,
                            series_source_id,
                            source_checkpoint_id
                        FROM work_metadata
                        WHERE work_id = ?
                        """,
                        (inspection.work_id,),
                    ).fetchone()
                if metadata is None:
                    diagnostics.append(
                        f"work_metadata row is missing for {inspection.work_id!r}"
                    )
                else:
                    lineage_fields = _master_lineage_fields(
                        universe_source_id=metadata["universe_source_id"],
                        series_source_id=metadata["series_source_id"],
                        source_checkpoint_id=metadata["source_checkpoint_id"],
                    )
                    if lineage_fields:
                        raise WorkIdentityMismatchError(
                            _unsnapshotted_master_lineage_message(lineage_fields)
                        )
        except Exception as exc:
            message = str(exc)
            if snapshot_root is not None:
                message = message.replace(str(snapshot_root), str(root))
            diagnostics.append(f"{type(exc).__name__}: {message}")

        return not diagnostics, tuple(diagnostics)

    def list_works(self) -> tuple[WorkCatalogEntry, ...]:
        """List Master-side Work discovery metadata."""
        with repository_read(self.paths.master_db) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM work_catalog
                ORDER BY updated_at DESC, work_id ASC
                """
            ).fetchall()
        return tuple(_catalog_entry(row) for row in rows)

    def _resolve_catalog_path(self, relative_work_path: str) -> Path:
        relative = Path(relative_work_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkIdentityMismatchError(
                f"unsafe catalog work path: {relative_work_path!r}"
            )
        root = (self.storage_root / relative).resolve()
        works_root = self.paths.works.resolve()
        if root == works_root or works_root not in root.parents:
            raise WorkIdentityMismatchError(
                f"catalog work path escapes works root: {relative_work_path!r}"
            )
        return root


__all__ = [
    "LEGACY_WORK_MANIFEST_FORMAT_VERSIONS",
    "LIVE_MANIFEST_HASH_POLICY",
    "LIVE_MANIFEST_INTEGRITY_MODE",
    "WORK_MANIFEST_FORMAT",
    "WORK_MANIFEST_FORMAT_VERSION",
    "WORK_RECOVERY_QUARANTINE_DIR",
    "WORK_STAGING_PREFIX",
    "PortableWorkInspection",
    "WorkCatalogEntry",
    "WorkCreationError",
    "WorkHandle",
    "WorkIdentityMismatchError",
    "WorkLifecycleError",
    "WorkLifecycleRepository",
    "WorkManifestError",
    "WorkNotFoundError",
    "WorkRecoveryError",
    "WorkRecoveryFinding",
    "WorkUpgradeError",
    "inspect_work_directory",
]
