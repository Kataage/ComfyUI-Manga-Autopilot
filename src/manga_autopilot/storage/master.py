"""Master database bootstrap and identity metadata helpers."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manga_autopilot.storage.migrations import (
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
    """Migrate a Master DB and initialize stable identity metadata if needed."""
    if database_id is not None and not database_id.strip():
        raise ValueError("database_id must be non-empty when provided")

    try:
        migration = migrate_master_database(
            database_path,
            app_version=app_version,
        )
    except DatabaseIdentityMismatchError as exc:
        raise MasterDatabaseIdentityError(
            f"database_kind mismatch: {exc}"
        ) from exc

    requested_database_id = database_id or _new_master_database_id()
    created_at = _utc_now_iso()

    with write_connection(database_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = _read_metadata(connection)

            existing_kind = existing.get("database_kind")
            accepted_kinds = {MASTER_DATABASE_KIND, *LEGACY_MASTER_DATABASE_KINDS}
            if existing_kind is not None and existing_kind not in accepted_kinds:
                raise MasterDatabaseIdentityError(
                    "database_kind mismatch: "
                    f"expected {MASTER_DATABASE_KIND!r}, got {existing_kind!r}"
                )

            existing_format = existing.get("format_version")
            if existing_format is not None and existing_format != MASTER_FORMAT_VERSION:
                raise MasterDatabaseIdentityError(
                    "format_version mismatch: "
                    f"expected {MASTER_FORMAT_VERSION!r}, got {existing_format!r}"
                )

            existing_database_id = existing.get("database_id")
            if (
                database_id is not None
                and existing_database_id is not None
                and existing_database_id != database_id
            ):
                raise MasterDatabaseIdentityError(
                    "database_id mismatch: "
                    f"expected {database_id!r}, got {existing_database_id!r}"
                )

            values = {
                "database_kind": MASTER_DATABASE_KIND,
                "database_id": existing_database_id or requested_database_id,
                "format_version": MASTER_FORMAT_VERSION,
                "created_at": existing.get("created_at") or created_at,
            }
            connection.executemany(
                """
                INSERT OR IGNORE INTO master_metadata (key, value)
                VALUES (?, ?)
                """,
                tuple(values.items()),
            )
            connection.execute(
                """
                UPDATE master_metadata
                SET value = ?
                WHERE key = 'database_kind' AND value != ?
                """,
                (MASTER_DATABASE_KIND, MASTER_DATABASE_KIND),
            )

            identity = _identity_from_metadata(_read_metadata(connection))
            connection.commit()
        except Exception:
            connection.rollback()
            raise

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
