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
    ensure_storage_root,
    migrate_work_database,
    read_work_identity,
    repository_read,
    repository_write,
    storage_paths,
    validate_work_database,
    validate_work_id,
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
    database_validation = validate_work_database(
        database_path,
        migrations=migration_set,
    )
    database_schema_version = database_validation.current_version

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
        staging_root = self.paths.works / f"{WORK_STAGING_PREFIX}{resolved_work_id}"

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
            assert_managed_path(
                staging_paths.root,
                containment_root=self.paths.works,
                field_name="Work staging directory",
            )
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
            expected_work_id=work_id,
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

    def scan_recovery(self) -> tuple[WorkRecoveryFinding, ...]:
        """Detect stale staging and unregistered Work directories without mutation."""
        with repository_read(self.paths.master_db) as connection:
            cataloged_ids = {
                str(row["work_id"])
                for row in connection.execute(
                    "SELECT work_id FROM work_catalog"
                ).fetchall()
            }

        findings: list[WorkRecoveryFinding] = []
        for entry in sorted(self.paths.works.iterdir(), key=lambda path: path.name):
            if entry.name == WORK_RECOVERY_QUARANTINE_DIR:
                continue

            if entry.name.startswith(WORK_STAGING_PREFIX):
                work_id = entry.name[len(WORK_STAGING_PREFIX) :] or None
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

        if not staging.exists() and not staging.is_symlink():
            if final_paths.root.exists() and not final_paths.root.is_symlink():
                return self.reconcile_orphan_work(work_id)
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
                f"final Work path already exists for {work_id!r}: {final_paths.root}"
            )

        os.replace(staging, final_paths.root)
        # If catalog registration fails, leave the finalized Work intact so a
        # later recovery scan can reconcile it without regenerating identity.
        return self.reconcile_orphan_work(work_id)

    def quarantine_staging_work(
        self,
        work_id: str,
        *,
        reason: str | None = None,
    ) -> Path:
        """Move stale staging aside without deleting its evidence."""
        work_paths(self.storage_root, work_id)  # validates one safe component
        staging = self.paths.works / f"{WORK_STAGING_PREFIX}{work_id}"
        if not staging.exists() and not staging.is_symlink():
            raise WorkRecoveryError(
                f"staging Work does not exist for {work_id!r}: {staging}"
            )

        quarantine_root = self.paths.works / WORK_RECOVERY_QUARANTINE_DIR
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
                f"recovery quarantine must not be a symlink: {quarantine_root}"
            )
        quarantine_root.mkdir(exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = quarantine_root / f"{stamp}-{WORK_STAGING_PREFIX[1:]}{work_id}"
        try:
            assert_managed_path(
                destination,
                containment_root=quarantine_root,
                field_name="recovery quarantine destination",
            )
        except ValueError as exc:
            raise WorkRecoveryError(str(exc)) from exc
        os.replace(staging, destination)

        _write_json_atomic(
            destination.with_name(destination.name + ".recovery.json"),
            {
                "work_id": work_id,
                "source": staging.name,
                "quarantined_at": _utc_now_iso(),
                "reason": reason,
            },
        )
        return destination

    def _validate_recovery_directory(
        self,
        root: Path,
        *,
        expected_work_id: str | None,
    ) -> tuple[bool, tuple[str, ...]]:
        diagnostics: list[str] = []
        if root.is_symlink():
            return False, (f"symlink recovery entry is not trusted: {root}",)
        if not root.is_dir():
            return False, (f"recovery entry is not a directory: {root}",)

        try:
            inspection = inspect_work_directory(
                root,
                migrations=self.work_migrations,
                expected_work_id=expected_work_id,
            )
            with repository_read(inspection.database_path) as connection:
                metadata = connection.execute(
                    "SELECT work_id FROM work_metadata WHERE work_id = ?",
                    (inspection.work_id,),
                ).fetchone()
            if metadata is None:
                diagnostics.append(
                    f"work_metadata row is missing for {inspection.work_id!r}"
                )
        except Exception as exc:
            diagnostics.append(f"{type(exc).__name__}: {exc}")

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
