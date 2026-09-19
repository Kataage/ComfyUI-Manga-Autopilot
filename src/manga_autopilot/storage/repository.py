"""Common repository transaction and optimistic-revision utilities.

These helpers intentionally keep transactions short and local. Callers must
perform LLM, ComfyUI, remote-provider, and long-running filesystem work before
entering a write transaction, then commit only the resulting formal state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manga_autopilot.storage.sqlite import read_connection, write_connection


class PersistenceError(RuntimeError):
    """Base class for v2 persistence-layer errors."""


class RevisionConflictError(PersistenceError):
    """Raised when optimistic revision validation fails."""

    def __init__(
        self,
        *,
        entity_type: str,
        entity_id: str,
        expected_revision: int,
        actual_revision: int,
    ) -> None:
        super().__init__(
            f"{entity_type} {entity_id!r} revision conflict: "
            f"expected {expected_revision}, actual {actual_revision}"
        )
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class TransactionRequiredError(PersistenceError):
    """Raised when a mutation helper is used outside an explicit transaction."""


@dataclass(frozen=True)
class WorkCommit:
    """Persisted Work commit identity returned by the commit helper."""

    commit_seq: int
    commit_id: str
    created_at: str


@dataclass(frozen=True)
class MasterCommit:
    """Persisted Master commit identity returned by the commit helper."""

    commit_seq: int
    commit_id: str
    created_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def assert_expected_revision(
    *,
    entity_type: str,
    entity_id: str,
    expected_revision: int,
    actual_revision: int,
) -> None:
    """Raise a structured conflict when optimistic revision validation fails."""
    if expected_revision < 0:
        raise ValueError("expected_revision must be >= 0")
    if actual_revision < 0:
        raise ValueError("actual_revision must be >= 0")
    if expected_revision != actual_revision:
        raise RevisionConflictError(
            entity_type=entity_type,
            entity_id=entity_id,
            expected_revision=expected_revision,
            actual_revision=actual_revision,
        )


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise TransactionRequiredError(
            "mutation helper requires an active explicit write transaction"
        )


@contextmanager
def repository_read(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a read-only repository connection."""
    with read_connection(database_path) as connection:
        yield connection


@contextmanager
def repository_write(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield one short explicit write transaction.

    The transaction is committed on normal exit and rolled back on any
    exception. No retry, network call, or external side effect is performed by
    this helper.
    """
    with write_connection(database_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def create_work_commit(
    connection: sqlite3.Connection,
    *,
    commit_id: str,
    actor_type: str,
    operation_type: str,
    parent_commit_seq: int | None = None,
    run_id: str | None = None,
    actor_id: str | None = None,
    reason: str | None = None,
    created_at: str | None = None,
) -> WorkCommit:
    """Insert one Work commit inside the caller's active transaction."""
    _require_transaction(connection)
    if not commit_id.strip():
        raise ValueError("commit_id must be non-empty")
    if not actor_type.strip():
        raise ValueError("actor_type must be non-empty")
    if not operation_type.strip():
        raise ValueError("operation_type must be non-empty")

    timestamp = created_at or _utc_now_iso()
    cursor = connection.execute(
        """
        INSERT INTO commits (
            commit_id,
            parent_commit_seq,
            run_id,
            actor_type,
            actor_id,
            operation_type,
            reason,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            commit_id,
            parent_commit_seq,
            run_id,
            actor_type,
            actor_id,
            operation_type,
            reason,
            timestamp,
        ),
    )
    return WorkCommit(
        commit_seq=int(cursor.lastrowid),
        commit_id=commit_id,
        created_at=timestamp,
    )


def create_master_commit(
    connection: sqlite3.Connection,
    *,
    commit_id: str,
    actor_type: str,
    actor_id: str | None = None,
    reason: str | None = None,
    run_or_operation_id: str | None = None,
    created_at: str | None = None,
) -> MasterCommit:
    """Insert one Master commit inside the caller's active transaction."""
    _require_transaction(connection)
    if not commit_id.strip():
        raise ValueError("commit_id must be non-empty")
    if not actor_type.strip():
        raise ValueError("actor_type must be non-empty")

    timestamp = created_at or _utc_now_iso()
    cursor = connection.execute(
        """
        INSERT INTO master_commits (
            commit_id,
            actor_type,
            actor_id,
            reason,
            run_or_operation_id,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            commit_id,
            actor_type,
            actor_id,
            reason,
            run_or_operation_id,
            timestamp,
        ),
    )
    return MasterCommit(
        commit_seq=int(cursor.lastrowid),
        commit_id=commit_id,
        created_at=timestamp,
    )


__all__ = [
    "MasterCommit",
    "PersistenceError",
    "RevisionConflictError",
    "TransactionRequiredError",
    "WorkCommit",
    "assert_expected_revision",
    "create_master_commit",
    "create_work_commit",
    "repository_read",
    "repository_write",
]
