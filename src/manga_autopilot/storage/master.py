"""Master database bootstrap and identity metadata helpers."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manga_autopilot.storage.migrations import (
    MASTER_MIGRATIONS,
    DatabaseIdentityMismatchError,
    MigrationResult,
    migrate_master_database,
)
from manga_autopilot.storage.sqlite import read_connection, write_connection

MASTER_DATABASE_KIND = "manga_autopilot_master"
LEGACY_MASTER_DATABASE_KINDS = frozenset({"master"})
MASTER_FORMAT_VERSION = "2"

_REQUIRED_METADATA_KEYS = (
    "database_kind",
    "database_id",
    "format_version",
    "created_at",
)


class MasterDatabaseIdentityError(RuntimeError):
    """Raised when Master DB identity metadata is missing or inconsistent."""


@dataclass(frozen=True)
class MasterDatabaseIdentity:
    """Stable identity metadata for one Master database."""

    database_kind: str
    database_id: str
    format_version: str
    created_at: str


@dataclass(frozen=True)
class MasterDatabaseBootstrapResult:
    """Result of bootstrapping or reopening a Master database."""

    identity: MasterDatabaseIdentity
    migration: MigrationResult


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_master_database_id() -> str:
    return f"master_{uuid.uuid4().hex}"


def _read_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT key, value
        FROM master_metadata
        WHERE key IN ('database_kind', 'database_id', 'format_version', 'created_at')
        """
    ).fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _identity_from_metadata(metadata: dict[str, str]) -> MasterDatabaseIdentity:
    missing = [key for key in _REQUIRED_METADATA_KEYS if not metadata.get(key)]
    if missing:
        raise MasterDatabaseIdentityError(
            "Master database identity metadata is incomplete: "
            + ", ".join(sorted(missing))
        )

    accepted_kinds = {MASTER_DATABASE_KIND, *LEGACY_MASTER_DATABASE_KINDS}
    if metadata["database_kind"] not in accepted_kinds:
        raise MasterDatabaseIdentityError(
            "database_kind mismatch: "
            f"expected {MASTER_DATABASE_KIND!r}, got {metadata['database_kind']!r}"
        )
    if metadata["format_version"] != MASTER_FORMAT_VERSION:
        raise MasterDatabaseIdentityError(
            "format_version mismatch: "
            f"expected {MASTER_FORMAT_VERSION!r}, got {metadata['format_version']!r}"
        )

    return MasterDatabaseIdentity(
        database_kind=MASTER_DATABASE_KIND,
        database_id=metadata["database_id"],
        format_version=metadata["format_version"],
        created_at=metadata["created_at"],
    )


def _migration_history_is_known_prefix(connection: sqlite3.Connection) -> bool:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "schema_migrations" not in tables or "master_metadata" not in tables:
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

    configured = {migration.version: migration for migration in MASTER_MIGRATIONS}
    for row in rows:
        version = int(row["version"])
        migration = configured.get(version)
        if migration is None:
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
    database_id: str,
    created_at: str,
) -> bool:
    """Recover only the exact empty-data state left by old Phase A bootstrap."""
    path = Path(database_path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        return False

    try:
        with read_connection(path) as connection:
            if not _migration_history_is_known_prefix(connection):
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
                "master_commits",
                "master_entity_revisions",
                "work_catalog",
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
                for table in (
                    "master_commits",
                    "master_entity_revisions",
                    "work_catalog",
                )
            ):
                connection.rollback()
                return False

            connection.executemany(
                """
                INSERT INTO master_metadata (key, value)
                VALUES (?, ?)
                """,
                (
                    ("database_kind", MASTER_DATABASE_KIND),
                    ("database_id", database_id),
                    ("format_version", MASTER_FORMAT_VERSION),
                    ("created_at", created_at),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return True


def read_master_identity(database_path: str | Path) -> MasterDatabaseIdentity:
    """Read and validate required Master database identity metadata."""
    with read_connection(database_path) as connection:
        metadata = _read_metadata(connection)
    return _identity_from_metadata(metadata)


def bootstrap_master_database(
    database_path: str | Path,
    *,
    database_id: str | None = None,
    app_version: str | None = None,
) -> MasterDatabaseBootstrapResult:
    """Migrate and initialize Master identity as one crash-safe operation."""
    if database_id is not None and not database_id.strip():
        raise ValueError("database_id must be non-empty when provided")

    requested_database_id = database_id or _new_master_database_id()
    created_at = _utc_now_iso()

    _recover_interrupted_identity_bootstrap(
        database_path,
        database_id=requested_database_id,
        created_at=created_at,
    )

    try:
        migration = migrate_master_database(
            database_path,
            app_version=app_version,
            database_id=requested_database_id,
            created_at=created_at,
        )
    except DatabaseIdentityMismatchError as exc:
        raise MasterDatabaseIdentityError(
            f"database_kind mismatch: {exc}"
        ) from exc

    identity = read_master_identity(database_path)
    if database_id is not None and identity.database_id != database_id:
        raise MasterDatabaseIdentityError(
            "database_id mismatch: "
            f"expected {database_id!r}, got {identity.database_id!r}"
        )

    return MasterDatabaseBootstrapResult(
        identity=identity,
        migration=migration,
    )


__all__ = [
    "MASTER_DATABASE_KIND",
    "LEGACY_MASTER_DATABASE_KINDS",
    "MASTER_FORMAT_VERSION",
    "MasterDatabaseBootstrapResult",
    "MasterDatabaseIdentity",
    "MasterDatabaseIdentityError",
    "bootstrap_master_database",
    "read_master_identity",
]
