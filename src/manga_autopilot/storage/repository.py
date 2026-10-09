"""Common repository transaction and optimistic-revision utilities.

These helpers intentionally keep transactions short and local. Callers must
perform LLM, ComfyUI, remote-provider, and long-running filesystem work before
entering a write transaction, then commit only the resulting formal state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manga_autopilot.primitives import canonical_json
from manga_autopilot.storage.sqlite import read_connection, write_connection


class PersistenceError(RuntimeError):
    """Base class for v2 persistence-layer errors."""


class WorkMutationLeaseConflictError(PersistenceError):
    """A Work mutation conflicts with its exclusive durable writer lease."""


# Bound only by trusted Work-scoped orchestrator invocations, never by
# request-controlled HTTP parameters. asyncio task creation and to_thread()
# copy this context, while independent HTTP tasks do not inherit the token.
_WORK_MUTATION_OWNER: ContextVar[tuple[str, str] | None] = ContextVar(
    "work_mutation_lease_owner", default=None,
)


@contextmanager
def owned_work_mutation(work_id: str, lease_owner: str) -> Iterator[None]:
    """Allow a live durable Work owner to make Work commits in its hook.

    Merely presenting a token is insufficient: the same write transaction
    revalidates the Work lease expiry and the RUNNING Run ownership.
    """
    if not work_id or not lease_owner:
        raise ValueError("work_id and lease_owner are required")
    token = _WORK_MUTATION_OWNER.set((work_id, lease_owner))
    try:
        yield
    finally:
        _WORK_MUTATION_OWNER.reset(token)


def assert_work_mutation_allowed(connection: sqlite3.Connection) -> None:
    """Fence all Work commit and Artifact publication paths under one lease.

    Must be called again INSIDE the final BEGIN IMMEDIATE Work transaction.
    Expired lease rows remain blocking tombstones until explicit recovery.
    A scoped old worker cannot write after its token has been released.
    """
    scope = _WORK_MUTATION_OWNER.get()
    # Initial pre-W0006 Work migration/bootstrap has no lease schema.
    schema = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'work_leases'"
    ).fetchone()
    if schema is None:
        if scope is not None:
            raise WorkMutationLeaseConflictError(
                "Work mutation lease schema is missing for scoped owner"
            )
        return
    lease = connection.execute(
        "SELECT work_id, lease_owner, run_id, expires_at FROM work_leases LIMIT 1"
    ).fetchone()
    if lease is None:
        if scope is not None:
            raise WorkMutationLeaseConflictError(
                "Work mutation lease was released or lost"
            )
        return
    if scope is None or tuple(scope) != (lease["work_id"], lease["lease_owner"]):
        raise WorkMutationLeaseConflictError(
            "Work mutation blocked by another or unreconciled lease"
        )
    try:
        expiry = datetime.fromisoformat(lease["expires_at"])
        valid_expiry = (
            expiry.tzinfo is not None
            and expiry.astimezone(timezone.utc) > datetime.now(timezone.utc)
        )
    except (TypeError, ValueError):
        valid_expiry = False
    if not valid_expiry:
        raise WorkMutationLeaseConflictError(
            "Work mutation lease expired; explicit recovery required"
        )
    if lease["run_id"] is not None:
        run = connection.execute(
            "SELECT status, lease_owner, scope_type, scope_id FROM runs WHERE id = ?",
            (lease["run_id"],),
        ).fetchone()
        if (
            run is None or run["status"] != "RUNNING"
            or run["lease_owner"] != scope[1] or run["scope_type"] != "WORK"
            or run["scope_id"] != scope[0]
        ):
            raise WorkMutationLeaseConflictError(
                "Work mutation lease is not attached to a live owned Run"
            )


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


@dataclass(frozen=True)
class WorkEntityRevision:
    """Persisted entity revision identity and canonical state snapshots."""

    revision_id: str
    entity_type: str
    entity_id: str
    entity_revision: int
    commit_seq: int
    change_kind: str
    before_json: str | None
    after_json: str | None
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
    assert_work_mutation_allowed(connection)
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


def create_work_entity_revision(
    connection: sqlite3.Connection,
    *,
    revision_id: str,
    entity_type: str,
    entity_id: str,
    entity_revision: int,
    commit_seq: int,
    change_kind: str,
    before_state: Any | None = None,
    after_state: Any | None = None,
    created_at: str | None = None,
) -> WorkEntityRevision:
    """Insert one canonical entity revision inside the active transaction."""
    _require_transaction(connection)
    for field_name, value in (
        ("revision_id", revision_id),
        ("entity_type", entity_type),
        ("entity_id", entity_id),
        ("change_kind", change_kind),
    ):
        if not value.strip():
            raise ValueError(f"{field_name} must be non-empty")
    if entity_revision <= 0:
        raise ValueError("entity_revision must be > 0")
    if commit_seq <= 0:
        raise ValueError("commit_seq must be > 0")

    timestamp = created_at or _utc_now_iso()
    before_json = (
        canonical_json(before_state)
        if before_state is not None
        else None
    )
    after_json = (
        canonical_json(after_state)
        if after_state is not None
        else None
    )
    connection.execute(
        """
        INSERT INTO entity_revisions (
            id,
            entity_type,
            entity_id,
            entity_revision,
            commit_seq,
            change_kind,
            before_json,
            after_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            entity_type,
            entity_id,
            entity_revision,
            commit_seq,
            change_kind,
            before_json,
            after_json,
            timestamp,
        ),
    )
    return WorkEntityRevision(
        revision_id=revision_id,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_revision=entity_revision,
        commit_seq=commit_seq,
        change_kind=change_kind,
        before_json=before_json,
        after_json=after_json,
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
    "WorkEntityRevision",
    "MasterCommit",
    "PersistenceError",
    "RevisionConflictError",
    "TransactionRequiredError",
    "WorkCommit",
    "assert_expected_revision",
    "create_work_entity_revision",
    "create_master_commit",
    "create_work_commit",
    "repository_read",
    "repository_write",
]
