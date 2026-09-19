"""v2 Work lifecycle repository backed by Master/Work SQLite databases."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manga_autopilot.primitives import new_id, sha256_file
from manga_autopilot.storage import (
    WORK_MIGRATIONS,
    Migration,
    MigrationError,
    WorkPaths,
    bootstrap_master_database,
    bootstrap_work_database,
    create_work_commit,
    ensure_storage_root,
    migrate_work_database,
    read_work_identity,
    repository_read,
    repository_write,
    storage_paths,
    work_paths,
    write_connection,
)

WORK_MANIFEST_FORMAT = "manga-autopilot-work"
WORK_MANIFEST_FORMAT_VERSION = 2
LEGACY_WORK_MANIFEST_FORMAT_VERSIONS = frozenset({1})
LIVE_MANIFEST_INTEGRITY_MODE = "live_mutable"
LIVE_MANIFEST_HASH_POLICY = "package_only"


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _read_manifest(path: Path) -> dict[str, Any]:
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


def _read_database_schema_version(database_path: Path) -> int:
    with repository_read(database_path) as connection:
        row = connection.execute(
            "SELECT MAX(version) AS version FROM schema_migrations"
        ).fetchone()
    return int(row["version"] or 0)


def _live_manifest(
    *,
    work_id: str,
    database_name: str,
    work_schema_version: int,
    created_at: str,
    app_version: str | None,
) -> dict[str, Any]:
    return {
        "format": WORK_MANIFEST_FORMAT,
        "format_version": WORK_MANIFEST_FORMAT_VERSION,
        "work_id": work_id,
        "database": database_name,
        "work_schema_version": work_schema_version,
        "created_at": created_at,
        "app_version": app_version,
        "integrity": {
            "mode": LIVE_MANIFEST_INTEGRITY_MODE,
            "database_hash_policy": LIVE_MANIFEST_HASH_POLICY,
        },
    }


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    expected_work_id: str | None,
    expected_database_name: str = "work.sqlite3",
) -> None:
    if manifest.get("format") != WORK_MANIFEST_FORMAT:
        raise WorkManifestError(
            f"unsupported Work manifest format: {manifest.get('format')!r}"
        )

    format_version = manifest.get("format_version")
    accepted_versions = {
        WORK_MANIFEST_FORMAT_VERSION,
        *LEGACY_WORK_MANIFEST_FORMAT_VERSIONS,
    }
    if format_version not in accepted_versions:
        raise WorkManifestError(
            f"unsupported Work manifest format_version: {format_version!r}"
        )

    work_id = manifest.get("work_id")
    if not isinstance(work_id, str) or not work_id:
        raise WorkManifestError("Work manifest work_id must be a non-empty string")
    if expected_work_id is not None and work_id != expected_work_id:
        raise WorkIdentityMismatchError(
            f"manifest Work identity mismatch: expected {expected_work_id!r}, "
            f"got {work_id!r}"
        )

    if manifest.get("database") != expected_database_name:
        raise WorkManifestError(
            f"manifest database mismatch: expected {expected_database_name!r}, "
            f"got {manifest.get('database')!r}"
        )

    schema_version = manifest.get("work_schema_version")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise WorkManifestError(
            "Work manifest work_schema_version must be a positive integer"
        )


def inspect_work_directory(
    work_root: str | Path,
    *,
    migrations: Iterable[Migration] = WORK_MIGRATIONS,
) -> PortableWorkInspection:
    """Inspect a portable Work directory without requiring Master DB access."""
    migration_set = tuple(migrations)
    root = Path(work_root).expanduser().resolve()
    manifest_path = root / "manifest.json"
    database_path = root / "work.sqlite3"

    manifest = _read_manifest(manifest_path)
    _validate_manifest(
        manifest,
        expected_work_id=None,
        expected_database_name=database_path.name,
    )

    work_id = str(manifest["work_id"])
    identity = read_work_identity(database_path)
    if identity.work_id != work_id:
        raise WorkIdentityMismatchError(
            f"Work DB identity mismatch: manifest={work_id!r}, "
            f"database={identity.work_id!r}"
        )

    database_schema_version = _read_database_schema_version(database_path)
    manifest_schema_version = int(manifest["work_schema_version"])
    if manifest_schema_version > database_schema_version:
        raise WorkManifestError(
            f"manifest schema version is ahead of Work DB for {work_id!r}: "
            f"manifest={manifest_schema_version}, database={database_schema_version}"
        )

    target_schema_version = _target_schema_version(migration_set)
    integrity = manifest.get("integrity")
    live_integrity_current = (
        isinstance(integrity, dict)
        and integrity.get("mode") == LIVE_MANIFEST_INTEGRITY_MODE
        and integrity.get("database_hash_policy") == LIVE_MANIFEST_HASH_POLICY
        and "database_sha256" not in integrity
    )
    manifest_refresh_required = (
        manifest.get("format_version") != WORK_MANIFEST_FORMAT_VERSION
        or manifest_schema_version != database_schema_version
        or not live_integrity_current
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
        """Create a self-contained Work and register it in the Master catalog."""
        for field_name, value in (
            ("title", title),
            ("work_kind", work_kind),
            ("language", language),
            ("reading_direction", reading_direction),
            ("status", status),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must be non-empty")

        resolved_work_id = work_id or new_id("work")
        final_paths = work_paths(self.storage_root, resolved_work_id)
        relative_work_path = final_paths.root.relative_to(self.storage_root).as_posix()
        staging_root = self.paths.works / f".creating-{resolved_work_id}"

        if final_paths.root.exists():
            raise WorkCreationError(f"Work directory already exists: {final_paths.root}")
        if staging_root.exists():
            raise WorkCreationError(
                f"staging directory already exists for Work {resolved_work_id!r}: "
                f"{staging_root}"
            )

        with repository_read(self.paths.master_db) as connection:
            existing = connection.execute(
                "SELECT 1 FROM work_catalog WHERE work_id = ?",
                (resolved_work_id,),
            ).fetchone()
        if existing is not None:
            raise WorkCreationError(
                f"Work already exists in Master catalog: {resolved_work_id}"
            )

        staging_paths = WorkPaths(work_id=resolved_work_id, root=staging_root)
        created_at = _utc_now_iso()

        try:
            staging_paths.root.mkdir(parents=True, exist_ok=False)
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

            with write_connection(staging_paths.work_db) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            manifest = _live_manifest(
                work_id=resolved_work_id,
                database_name=staging_paths.work_db.name,
                work_schema_version=_target_schema_version(self.work_migrations),
                created_at=created_at,
                app_version=self.app_version,
            )
            _write_json_atomic(staging_paths.manifest_json, manifest)
            manifest_hash = sha256_file(staging_paths.manifest_json)

            os.replace(staging_paths.root, final_paths.root)

            try:
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
                shutil.rmtree(final_paths.root, ignore_errors=True)
                raise

        except Exception as exc:
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
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
        root = self._resolve_catalog_path(catalog.relative_work_path)
        inspection = inspect_work_directory(
            root,
            migrations=self.work_migrations,
        )
        if inspection.work_id != work_id:
            raise WorkIdentityMismatchError(
                f"portable Work identity mismatch: catalog={work_id!r}, "
                f"work={inspection.work_id!r}"
            )

        manifest = _read_manifest(inspection.manifest_path)

        try:
            migration = migrate_work_database(
                inspection.database_path,
                migrations=self.work_migrations,
                app_version=self.app_version,
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
    "PortableWorkInspection",
    "WorkCatalogEntry",
    "WorkCreationError",
    "WorkHandle",
    "WorkIdentityMismatchError",
    "WorkLifecycleError",
    "WorkLifecycleRepository",
    "WorkManifestError",
    "WorkNotFoundError",
    "WorkUpgradeError",
    "inspect_work_directory",
]
