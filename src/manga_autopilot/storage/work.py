"""Work database bootstrap and identity metadata helpers."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manga_autopilot.storage.migrations import (
    DatabaseIdentityMismatchError,
    MigrationResult,
    migrate_work_database,
)
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
) -> WorkDatabaseBootstrapResult:
    """Migrate a Work DB and initialize stable identity metadata if needed."""
    if not work_id.strip():
        raise ValueError("work_id must be non-empty")
    if database_id is not None and not database_id.strip():
        raise ValueError("database_id must be non-empty when provided")

    try:
        migration = migrate_work_database(
            database_path,
            app_version=app_version,
        )
    except DatabaseIdentityMismatchError as exc:
        raise WorkDatabaseIdentityError(str(exc)) from exc

    requested_database_id = database_id or _new_work_database_id()
    created_at = _utc_now_iso()

    with write_connection(database_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = _read_metadata(connection)

            existing_kind = existing.get("database_kind")
            if existing_kind is not None and existing_kind != WORK_DATABASE_KIND:
                raise WorkDatabaseIdentityError(
                    "database_kind mismatch: "
                    f"expected {WORK_DATABASE_KIND!r}, got {existing_kind!r}"
                )

            existing_format = existing.get("format_version")
            if existing_format is not None and existing_format != WORK_FORMAT_VERSION:
                raise WorkDatabaseIdentityError(
                    "format_version mismatch: "
                    f"expected {WORK_FORMAT_VERSION!r}, got {existing_format!r}"
                )

            existing_work_id = existing.get("work_id")
            if existing_work_id is not None and existing_work_id != work_id:
                raise WorkDatabaseIdentityError(
                    "work_id mismatch: "
                    f"expected {work_id!r}, got {existing_work_id!r}"
                )

            existing_database_id = existing.get("database_id")
            if (
                database_id is not None
                and existing_database_id is not None
                and existing_database_id != database_id
            ):
                raise WorkDatabaseIdentityError(
                    "database_id mismatch: "
                    f"expected {database_id!r}, got {existing_database_id!r}"
                )

            values = {
                "database_kind": WORK_DATABASE_KIND,
                "database_id": existing_database_id or requested_database_id,
                "format_version": WORK_FORMAT_VERSION,
                "work_id": existing_work_id or work_id,
                "created_at": existing.get("created_at") or created_at,
            }
            connection.executemany(
                """
                INSERT OR IGNORE INTO work_database_metadata (key, value)
                VALUES (?, ?)
                """,
                tuple(values.items()),
            )

            identity = _identity_from_metadata(_read_metadata(connection))
            connection.commit()
        except Exception:
            connection.rollback()
            raise

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
