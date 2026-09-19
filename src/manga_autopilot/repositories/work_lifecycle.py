"""v2 Work lifecycle repository backed by Master/Work SQLite databases."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manga_autopilot.primitives import new_id, sha256_file
from manga_autopilot.storage import (
    MASTER_MIGRATIONS,
    WORK_MIGRATIONS,
    WorkPaths,
    bootstrap_master_database,
    bootstrap_work_database,
    create_work_commit,
    ensure_storage_root,
    read_work_identity,
    repository_read,
    repository_write,
    storage_paths,
    work_paths,
)

WORK_MANIFEST_FORMAT = "manga-autopilot-work"
WORK_MANIFEST_FORMAT_VERSION = 1


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


class WorkLifecycleRepository:
    """Create, open, and discover v2 Works.

    The Master catalog is discovery metadata only. Opened Work semantic fields
    are always read from `work.sqlite3`.
    """

    def __init__(self, storage_root: str | Path, *, app_version: str | None = None) -> None:
        self.storage_root = ensure_storage_root(storage_root)
        self.paths = storage_paths(self.storage_root)
        self.app_version = app_version
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
        manifest_hash: str | None = None

        try:
            staging_paths.root.mkdir(parents=True, exist_ok=False)
            staging_paths.assets.mkdir()
            staging_paths.cache.mkdir()
            staging_paths.exports.mkdir()

            bootstrap_work_database(
                staging_paths.work_db,
                work_id=resolved_work_id,
                app_version=self.app_version,
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

            with repository_write(staging_paths.work_db) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            manifest = {
                "format": WORK_MANIFEST_FORMAT,
                "format_version": WORK_MANIFEST_FORMAT_VERSION,
                "work_id": resolved_work_id,
                "database": staging_paths.work_db.name,
                "work_schema_version": WORK_MIGRATIONS[-1].version,
                "created_at": created_at,
                "app_version": self.app_version,
                "integrity": {
                    "database_sha256": sha256_file(staging_paths.work_db),
                },
            }
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
        """Open one cataloged Work and validate manifest/DB identity."""
        with repository_read(self.paths.master_db) as connection:
            row = connection.execute(
                "SELECT * FROM work_catalog WHERE work_id = ?",
                (work_id,),
            ).fetchone()
        if row is None:
            raise WorkNotFoundError(f"Work is not in Master catalog: {work_id}")

        catalog = _catalog_entry(row)
        root = self._resolve_catalog_path(catalog.relative_work_path)
        database_path = root / "work.sqlite3"
        manifest_path = root / "manifest.json"

        manifest = _read_manifest(manifest_path)
        self._validate_manifest(
            manifest,
            expected_work_id=work_id,
            expected_database_name=database_path.name,
        )

        current_manifest_hash = sha256_file(manifest_path)
        if (
            catalog.manifest_hash is not None
            and current_manifest_hash != catalog.manifest_hash
        ):
            raise WorkManifestError(
                f"manifest hash mismatch for Work {work_id!r}: "
                f"catalog={catalog.manifest_hash}, actual={current_manifest_hash}"
            )

        identity = read_work_identity(database_path)
        if identity.work_id != work_id:
            raise WorkIdentityMismatchError(
                f"Work DB identity mismatch: catalog={work_id!r}, "
                f"database={identity.work_id!r}"
            )

        with repository_read(database_path) as connection:
            metadata = connection.execute(
                "SELECT * FROM work_metadata WHERE work_id = ?",
                (work_id,),
            ).fetchone()
            schema_version_row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()

        if metadata is None:
            raise WorkIdentityMismatchError(
                f"Work DB has no authoritative work_metadata row for {work_id!r}"
            )

        manifest_schema_version = manifest.get("work_schema_version")
        actual_schema_version = int(schema_version_row["version"] or 0)
        if manifest_schema_version != actual_schema_version:
            raise WorkManifestError(
                f"work schema version mismatch for {work_id!r}: "
                f"manifest={manifest_schema_version!r}, actual={actual_schema_version}"
            )

        opened_at = _utc_now_iso()
        with repository_write(self.paths.master_db) as connection:
            connection.execute(
                """
                UPDATE work_catalog
                SET last_opened_at = ?
                WHERE work_id = ?
                """,
                (opened_at, work_id),
            )

        refreshed_catalog = WorkCatalogEntry(
            **{
                **catalog.__dict__,
                "last_opened_at": opened_at,
            }
        )
        return WorkHandle(
            work_id=work_id,
            root=root,
            database_path=database_path,
            manifest_path=manifest_path,
            title=str(metadata["title"]),
            work_kind=str(metadata["work_kind"]),
            language=str(metadata["language"]),
            reading_direction=str(metadata["reading_direction"]),
            status=str(metadata["status"]),
            current_commit_seq=int(metadata["current_commit_seq"]),
            current_revision=int(metadata["current_revision"]),
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

    @staticmethod
    def _validate_manifest(
        manifest: dict[str, Any],
        *,
        expected_work_id: str,
        expected_database_name: str,
    ) -> None:
        if manifest.get("format") != WORK_MANIFEST_FORMAT:
            raise WorkManifestError(
                f"unsupported Work manifest format: {manifest.get('format')!r}"
            )
        if manifest.get("format_version") != WORK_MANIFEST_FORMAT_VERSION:
            raise WorkManifestError(
                "unsupported Work manifest format_version: "
                f"{manifest.get('format_version')!r}"
            )
        if manifest.get("work_id") != expected_work_id:
            raise WorkIdentityMismatchError(
                f"manifest Work identity mismatch: expected {expected_work_id!r}, "
                f"got {manifest.get('work_id')!r}"
            )
        if manifest.get("database") != expected_database_name:
            raise WorkManifestError(
                f"manifest database mismatch: expected {expected_database_name!r}, "
                f"got {manifest.get('database')!r}"
            )


__all__ = [
    "WORK_MANIFEST_FORMAT",
    "WORK_MANIFEST_FORMAT_VERSION",
    "WorkCatalogEntry",
    "WorkCreationError",
    "WorkHandle",
    "WorkIdentityMismatchError",
    "WorkLifecycleError",
    "WorkLifecycleRepository",
    "WorkManifestError",
    "WorkNotFoundError",
]
