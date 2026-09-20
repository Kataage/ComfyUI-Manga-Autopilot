"""Work database bootstrap and identity metadata helpers."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manga_autopilot.storage.migrations import (
    WORK_MIGRATIONS,
    DatabaseIdentityMismatchError,
    Migration,
    MigrationResult,
    migrate_work_database,
)
from manga_autopilot.storage.paths import UnsafeStoragePathError
from manga_autopilot.storage.sqlite import read_connection, write_connection

WORK_DATABASE_KIND = "work"
WORK_FORMAT_VERSION = "2"

_REQUIRED_METADATA_KEYS = (
    "database_kind",
    "database_id",
    "format_version",
    "work_id",
    "created_at",
)


class WorkDatabaseIdentityError(RuntimeError):
    """Raised when Work DB identity metadata is missing or inconsistent."""


@dataclass(frozen=True)
class WorkDatabaseIdentity:
    """Stable identity metadata for one Work database."""

    database_kind: str
    database_id: str
    format_version: str
    work_id: str
    created_at: str


@dataclass(frozen=True)
class WorkDatabaseBootstrapResult:
    """Result of bootstrapping or reopening a Work database."""

    identity: WorkDatabaseIdentity
    migration: MigrationResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_work_database_id() -> str:
    return f"workdb_{uuid.uuid4().hex}"


def _read_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT key, value
        FROM work_database_metadata
        WHERE key IN (
            'database_kind',
            'database_id',
            'format_version',
            'work_id',
            'created_at'
        )
        """
    ).fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _identity_from_metadata(metadata: dict[str, str]) -> WorkDatabaseIdentity:
    missing = [key for key in _REQUIRED_METADATA_KEYS if not metadata.get(key)]
    if missing:
        raise WorkDatabaseIdentityError(
            "Work database identity metadata is incomplete: "
            + ", ".join(sorted(missing))
        )

    if metadata["database_kind"] != WORK_DATABASE_KIND:
        raise WorkDatabaseIdentityError(
            "database_kind mismatch: "
            f"expected {WORK_DATABASE_KIND!r}, got {metadata['database_kind']!r}"
        )
    if metadata["format_version"] != WORK_FORMAT_VERSION:
        raise WorkDatabaseIdentityError(
            "format_version mismatch: "
            f"expected {WORK_FORMAT_VERSION!r}, got {metadata['format_version']!r}"
        )

    return WorkDatabaseIdentity(
        database_kind=metadata["database_kind"],
        database_id=metadata["database_id"],
        format_version=metadata["format_version"],
        work_id=metadata["work_id"],
        created_at=metadata["created_at"],
    )


def _migration_history_is_known_prefix(
    connection: sqlite3.Connection,
    migrations: Iterable[Migration],
) -> bool:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "schema_migrations" not in tables or "work_database_metadata" not in tables:
        return False

    rows = connection.execute(
        """
        SELECT version, name, checksum
        FROM schema_migrations
        ORDER BY version
        """
    ).fetchall()
    if not rows:
        return False

    migration_set = tuple(migrations)
    if len(rows) > len(migration_set):
        return False

    for row, migration in zip(rows, migration_set, strict=False):
        if int(row["version"]) != migration.version:
            return False
        if str(row["name"]) != migration.name:
            return False
        if str(row["checksum"]) != migration.checksum:
            return False
    return True


def _table_is_empty(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
    return row is None


def _recover_interrupted_identity_bootstrap(
    database_path: str | Path,
    *,
    work_id: str,
    database_id: str,
    created_at: str,
    migrations: Iterable[Migration],
) -> bool:
    """Recover only an empty-data Work DB left by old Phase A bootstrap."""
    path = Path(database_path).expanduser().absolute()
    if path.is_symlink():
        raise UnsafeStoragePathError(
            f"database_path must not be a symlink: {path}"
        )
    if not path.is_file() or path.stat().st_size == 0:
        return False

    migration_set = tuple(migrations)
    try:
        with read_connection(path) as connection:
            if not _migration_history_is_known_prefix(connection, migration_set):
                return False

            metadata = _read_metadata(connection)
            if metadata:
                return False

            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            required_empty_tables = (
                "commits",
                "entity_revisions",
                "work_metadata",
            )
            if any(table not in tables for table in required_empty_tables):
                return False
            if not all(
                _table_is_empty(connection, table)
                for table in required_empty_tables
            ):
                return False
    except sqlite3.DatabaseError:
        return False

    with write_connection(path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            if _read_metadata(connection):
                connection.rollback()
                return False
            if not all(
                _table_is_empty(connection, table)
                for table in ("commits", "entity_revisions", "work_metadata")
            ):
                connection.rollback()
                return False

            connection.executemany(
                """
                INSERT INTO work_database_metadata (key, value)
                VALUES (?, ?)
                """,
                (
                    ("database_kind", WORK_DATABASE_KIND),
                    ("database_id", database_id),
                    ("format_version", WORK_FORMAT_VERSION),
                    ("work_id", work_id),
                    ("created_at", created_at),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return True


def read_work_identity(database_path: str | Path) -> WorkDatabaseIdentity:
    """Read and validate required Work database identity metadata."""
    with read_connection(database_path) as connection:
        metadata = _read_metadata(connection)
    return _identity_from_metadata(metadata)


def bootstrap_work_database(
    database_path: str | Path,
    *,
    work_id: str,
    database_id: str | None = None,
    app_version: str | None = None,
    migrations: Iterable[Migration] = WORK_MIGRATIONS,
) -> WorkDatabaseBootstrapResult:
    """Migrate and initialize Work identity as one crash-safe operation."""
    if not work_id.strip():
        raise ValueError("work_id must be non-empty")
    if database_id is not None and not database_id.strip():
        raise ValueError("database_id must be non-empty when provided")

    migration_set = tuple(migrations)
    requested_database_id = database_id or _new_work_database_id()
    created_at = _utc_now_iso()

    _recover_interrupted_identity_bootstrap(
        database_path,
        work_id=work_id,
        database_id=requested_database_id,
        created_at=created_at,
        migrations=migration_set,
    )

    try:
        migration = migrate_work_database(
            database_path,
            migrations=migration_set,
            app_version=app_version,
            work_id=work_id,
            database_id=requested_database_id,
            created_at=created_at,
        )
    except DatabaseIdentityMismatchError as exc:
        raise WorkDatabaseIdentityError(
            f"database_kind mismatch: {exc}"
        ) from exc

    identity = read_work_identity(database_path)
    if identity.work_id != work_id:
        raise WorkDatabaseIdentityError(
            "work_id mismatch: "
            f"expected {work_id!r}, got {identity.work_id!r}"
        )
    if database_id is not None and identity.database_id != database_id:
        raise WorkDatabaseIdentityError(
            "database_id mismatch: "
            f"expected {database_id!r}, got {identity.database_id!r}"
        )

    return WorkDatabaseBootstrapResult(
        identity=identity,
        migration=migration,
    )


__all__ = [
    "WORK_DATABASE_KIND",
    "WORK_FORMAT_VERSION",
    "WorkDatabaseBootstrapResult",
    "WorkDatabaseIdentity",
    "WorkDatabaseIdentityError",
    "bootstrap_work_database",
    "read_work_identity",
]
