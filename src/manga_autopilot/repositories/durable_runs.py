"""Durable Work-local Runs, RunSteps and single-writer mutation leases.

This is the persistence foundation (#245), not the Autopilot orchestrator
adapter (#246). Every mutation uses one short BEGIN IMMEDIATE transaction;
slow work and external IO must happen outside this repository.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from manga_autopilot.primitives import canonical_json, new_id
from manga_autopilot.storage.repository import (
    PersistenceError,
    repository_read,
    repository_write,
)


class DurableRunNotFoundError(PersistenceError):
    """A Run or RunStep does not exist in this Work."""


class DurableRunStateError(PersistenceError):
    """An illegal or stale Run/RunStep state transition was attempted."""


class WorkLeaseConflictError(PersistenceError):
    """A Work writer lease is occupied, expired without approval, or lost."""


_RUN_TRANSITIONS = {
    "PENDING": frozenset({"RUNNING", "CANCELLED"}),
    "RUNNING": frozenset({
        "PAUSED", "COMPLETED", "FAILED_RETRYABLE", "FAILED_TERMINAL",
        "INTERRUPTED", "NEEDS_ATTENTION", "CANCELLED",
    }),
    "PAUSED": frozenset({"RUNNING", "CANCELLED"}),
    "INTERRUPTED": frozenset({"RUNNING", "CANCELLED"}),
    "FAILED_RETRYABLE": frozenset({"RUNNING", "FAILED_TERMINAL", "CANCELLED"}),
    "NEEDS_ATTENTION": frozenset({"RUNNING", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "FAILED_TERMINAL": frozenset(),
    "CANCELLED": frozenset(),
}
_RUN_TERMINAL = frozenset({"COMPLETED", "FAILED_TERMINAL", "CANCELLED"})
_STEP_STARTABLE = frozenset({
    "PENDING", "INTERRUPTED", "FAILED_RETRYABLE", "STALE",
    "NEEDS_ATTENTION",
})
_STEP_TERMINAL = frozenset({
    "COMPLETED", "FAILED_RETRYABLE", "FAILED_TERMINAL",
    "INTERRUPTED", "NEEDS_ATTENTION", "CANCELLED",
})


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("clock must produce timezone-aware datetimes")
    return instant.astimezone(timezone.utc)


class DurableRunRepository:
    """Work DB-scoped durable progress and lease operations.

    A test clock may be injected, but application callers should use the
    default wall clock. Run/Step IDs remain stable; attempt_count advances
    on every new start. A completed step is never silently overwritten.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return _utc(self._clock())

    @staticmethod
    def _get(
        connection: sqlite3.Connection,
        table: str,
        identifier: str,
    ) -> sqlite3.Row:
        # Only fixed internal table identifiers are passed here.
        if table not in ("runs", "run_steps"):
            raise ValueError("unsupported table")
        row = connection.execute(
            f"SELECT * FROM {table} WHERE id = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise DurableRunNotFoundError(f"{table} {identifier!r} not found")
        return row

    @staticmethod
    def _assert_run_step_history_mutable(
        conn: sqlite3.Connection, run_id: str,
    ) -> None:
        """Keep terminal parent Run receipts immutable in this write transaction.

        Owner authorization alone is insufficient: a successfully released
        lease leaves a final Run available for generic unleased writes.
        """
        run = DurableRunRepository._get(conn, "runs", run_id)
        if run["status"] in _RUN_TERMINAL:
            raise DurableRunStateError(
                "cannot mutate Step history belonging to a terminal Run"
            )

    @staticmethod
    def _assert_unowned_mutation_allowed(conn: sqlite3.Connection) -> None:
        """Unleased generic Run operations cannot bypass any Work lease.

        This check must run inside the same BEGIN IMMEDIATE transaction as
        the mutation. An expired lease row is a blocking recovery tombstone,
        not permission for a new anonymous writer.
        """
        if conn.execute("SELECT 1 FROM work_leases LIMIT 1").fetchone() is not None:
            raise WorkLeaseConflictError(
                "durable mutation requires the current Work lease owner"
            )

    def create_run(
        self,
        *,
        run_kind: str,
        scope_type: str,
        requested_by: str,
        input_fingerprint: str,
        scope_id: str | None = None,
        parent_run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        for field, value in (
            ("run_kind", run_kind),
            ("scope_type", scope_type),
            ("requested_by", requested_by),
            ("input_fingerprint", input_fingerprint),
        ):
            _required(value, field)
        identifier = _required(run_id or new_id("run"), "run_id")
        timestamp = self._now().isoformat()
        encoded = canonical_json(dict(metadata or {}))
        with repository_write(self.database_path) as conn:
            self._assert_unowned_mutation_allowed(conn)
            conn.execute(
                """
                INSERT INTO runs (
                    id, run_kind, scope_type, scope_id, status,
                    requested_by, parent_run_id, lease_owner, heartbeat_at,
                    input_fingerprint, metadata_json, started_at,
                    finished_at, created_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, NULL, NULL,
                          ?, ?, NULL, NULL, ?)
                """,
                (identifier, run_kind, scope_type, scope_id, requested_by,
                 parent_run_id, input_fingerprint, encoded, timestamp),
            )
            return dict(self._get(conn, "runs", identifier))

    def get_run(self, run_id: str) -> dict[str, Any]:
        with repository_read(self.database_path) as conn:
            return dict(self._get(conn, "runs", run_id))

    def transition_run(
        self,
        run_id: str,
        *,
        expected_status: str,
        new_status: str,
        lease_owner: str | None = None,
    ) -> dict[str, Any]:
        if new_status not in _RUN_TRANSITIONS.get(expected_status, frozenset()):
            raise DurableRunStateError(
                f"illegal Run transition {expected_status} -> {new_status}"
            )
        timestamp = self._now().isoformat()
        terminal = new_status in {
            "COMPLETED", "FAILED_TERMINAL", "CANCELLED",
        }
        with repository_write(self.database_path) as conn:
            old = self._get(conn, "runs", run_id)
            if old["status"] != expected_status:
                raise DurableRunStateError(
                    f"Run {run_id!r} is {old['status']}, expected {expected_status}"
                )
            if lease_owner is None:
                self._assert_owner(conn, run_id, None)
            else:
                _required(lease_owner, "lease_owner")
                if old["lease_owner"] not in (None, lease_owner):
                    raise DurableRunStateError("Run lease owner mismatch")
                if expected_status == "RUNNING":
                    self._assert_owner(conn, run_id, lease_owner)
                else:
                    # A supplied token is never a wildcard for PENDING,
                    # PAUSED or other transitions. It must own this Run.
                    lease = conn.execute(
                        """SELECT expires_at FROM work_leases
                           WHERE run_id = ? AND lease_owner = ?""",
                        (run_id, lease_owner),
                    ).fetchone()
                    if (
                        lease is None
                        or _utc(datetime.fromisoformat(
                            lease["expires_at"]
                        )) <= self._now()
                    ):
                        raise WorkLeaseConflictError("no current lease to start Run")
            # A parent Run cannot leave RUNNING while any in-flight child
            # Step or Attempt is still RUNNING. Historical malformed rows
            # may have a terminal Step with a RUNNING attempt, so both
            # receipt tables must be checked. Preserve all rows on conflict
            # within this same BEGIN IMMEDIATE transaction.
            if expected_status == "RUNNING":
                active_step = conn.execute(
                    "SELECT 1 FROM run_steps "
                    "WHERE run_id = ? AND status = 'RUNNING' LIMIT 1",
                    (run_id,),
                ).fetchone()
                active_attempt = conn.execute(
                    "SELECT 1 FROM run_step_attempts "
                    "WHERE status = 'RUNNING' AND run_step_id IN "
                    "(SELECT id FROM run_steps WHERE run_id = ?) LIMIT 1",
                    (run_id,),
                ).fetchone()
                if active_step is not None or active_attempt is not None:
                    raise DurableRunStateError(
                        "cannot transition RUNNING Run with an active "
                        "Step or Attempt; reconcile durable receipts first"
                    )
                if new_status == "COMPLETED":
                    # A successful parent may only attest to registered
                    # children whose current outcome truly succeeded. Historic
                    # FAILED_RETRYABLE/INTERRUPTED attempts remain append-only
                    # evidence when a later attempt completed successfully.
                    incomplete_step = conn.execute(
                        "SELECT 1 FROM run_steps "
                        "WHERE run_id = ? AND status != 'COMPLETED' LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    if incomplete_step is not None:
                        raise DurableRunStateError(
                            "cannot complete Run with an unfinished or failed Step"
                        )
                    bad_latest_attempt = conn.execute(
                        "SELECT 1 FROM run_steps AS s "
                        "LEFT JOIN run_step_attempts AS a "
                        "ON a.run_step_id = s.id AND a.attempt_no = s.attempt_count "
                        "WHERE s.run_id = ? AND (s.attempt_count < 1 "
                        "OR a.id IS NULL OR a.status != 'COMPLETED' "
                        "OR a.input_fingerprint != s.input_fingerprint) LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    if bad_latest_attempt is not None:
                        raise DurableRunStateError(
                            "cannot complete Run without a successful final "
                            "Step Attempt receipt"
                        )
            conn.execute(
                """
                UPDATE runs SET status = ?,
                    lease_owner = ?,
                    heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?),
                    finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    new_status,
                    lease_owner if new_status == "RUNNING" else None,
                    timestamp if new_status == "RUNNING" else old["heartbeat_at"],
                    timestamp if new_status == "RUNNING" else old["started_at"],
                    timestamp if terminal else None,
                    run_id,
                    expected_status,
                ),
            )
            return dict(self._get(conn, "runs", run_id))

    def heartbeat_run(self, run_id: str, *, lease_owner: str) -> None:
        _required(lease_owner, "lease_owner")
        with repository_write(self.database_path) as conn:
            self._assert_owner(conn, run_id, lease_owner)
            cursor = conn.execute(
                """UPDATE runs SET heartbeat_at = ?
                   WHERE id = ? AND status = 'RUNNING' AND lease_owner = ?""",
                (self._now().isoformat(), run_id, lease_owner),
            )
            if cursor.rowcount != 1:
                raise DurableRunStateError("Run heartbeat owner/state mismatch")

    def _assert_owner(
        self, conn: sqlite3.Connection, run_id: str, owner: str | None,
    ) -> None:
        """Fence *all* durable Run/Step mutations within the write transaction.

        Ownerless mutations are supported only for unleased generic low-level
        operations, never while any Work lease/tombstone exists. A Run still
        marked as owned also cannot be silently edited after its lease vanished.
        """
        run = self._get(conn, "runs", run_id)
        if owner is None:
            self._assert_unowned_mutation_allowed(conn)
            if run["lease_owner"] is not None:
                raise WorkLeaseConflictError(
                    "owned durable Run cannot be mutated without its owner"
                )
            return
        _required(owner, "lease_owner")
        lease = conn.execute(
            "SELECT * FROM work_leases WHERE run_id = ? AND lease_owner = ?",
            (run_id, owner),
        ).fetchone()
        if (
            run["status"] != "RUNNING" or run["lease_owner"] != owner
            or lease is None
            or _utc(datetime.fromisoformat(lease["expires_at"])) <= self._now()
        ):
            raise WorkLeaseConflictError("stale or expired durable Run owner")

    def list_step_attempts(self, step_id: str) -> list[dict[str, Any]]:
        with repository_read(self.database_path) as conn:
            self._get(conn, "run_steps", step_id)
            return [
                dict(row) for row in conn.execute(
                    """SELECT * FROM run_step_attempts
                       WHERE run_step_id = ? ORDER BY attempt_no""",
                    (step_id,),
                ).fetchall()
            ]

    def recover_interrupted_run(self, run_id: str, *, lease_owner: str) -> None:
        """Fence a crashed process after explicit expired-lease recovery."""
        with repository_write(self.database_path) as conn:
            run = self._get(conn, "runs", run_id)
            if run["status"] != "RUNNING":
                raise DurableRunStateError("only RUNNING Run can be recovered")
            lease = conn.execute(
                "SELECT * FROM work_leases WHERE run_id = ? AND lease_owner = ?",
                (run_id, lease_owner),
            ).fetchone()
            if (
                lease is None
                or _utc(datetime.fromisoformat(lease["expires_at"])) <= self._now()
                or run["lease_owner"] is None
                or run["lease_owner"] == lease_owner
            ):
                # A fresh lease attached to an ownerless generic RUNNING Run
                # is not proof of a crashed, previously leased Run worker.
                raise WorkLeaseConflictError(
                    "recovery requires a prior Run owner and a new active lease"
                )
            timestamp = self._now().isoformat()
            conn.execute(
                """UPDATE run_step_attempts SET status = 'INTERRUPTED',
                    error_json = '{"reason":"process_interrupted"}',
                    finished_at = ?
                    WHERE status = 'RUNNING' AND run_step_id IN (
                        SELECT id FROM run_steps WHERE run_id = ?
                    )""",
                (timestamp, run_id),
            )
            conn.execute(
                """UPDATE run_steps SET status = 'INTERRUPTED',
                    error_json = '{"reason":"process_interrupted"}',
                    finished_at = ?
                    WHERE run_id = ? AND status = 'RUNNING'""",
                (timestamp, run_id),
            )
            conn.execute(
                """UPDATE runs SET status = 'INTERRUPTED',
                    lease_owner = NULL WHERE id = ?""", (run_id,),
            )

    def set_pending_fingerprint(
        self, step_id: str, *, input_fingerprint: str,
        lease_owner: str | None = None,
    ) -> None:
        _required(input_fingerprint, "input_fingerprint")
        with repository_write(self.database_path) as conn:
            step = self._get(conn, "run_steps", step_id)
            self._assert_owner(conn, str(step["run_id"]), lease_owner)
            self._assert_run_step_history_mutable(conn, str(step["run_id"]))
            if step["status"] != "PENDING":
                raise DurableRunStateError("only pending step can update its input")
            conn.execute(
                "UPDATE run_steps SET input_fingerprint = ? WHERE id = ?",
                (input_fingerprint, step_id),
            )

    def create_step(
        self,
        *,
        run_id: str,
        step_key: str,
        input_fingerprint: str,
        scope_type: str | None = None,
        scope_id: str | None = None,
        step_id: str | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any]:
        _required(step_key, "step_key")
        _required(input_fingerprint, "input_fingerprint")
        if (scope_type is None) != (scope_id is None):
            raise ValueError("step scope_type and scope_id must both be set or absent")
        if scope_type is not None:
            _required(scope_type, "scope_type")
            _required(scope_id, "scope_id")
        identifier = _required(step_id or new_id("step"), "step_id")
        with repository_write(self.database_path) as conn:
            self._get(conn, "runs", run_id)
            self._assert_owner(conn, run_id, lease_owner)
            self._assert_run_step_history_mutable(conn, run_id)
            conn.execute(
                """
                INSERT INTO run_steps (
                    id, run_id, step_key, scope_type, scope_id, status,
                    attempt_count, input_fingerprint, output_json, error_json,
                    started_at, finished_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, '{}', '{}',
                          NULL, NULL, NULL)
                """,
                (identifier, run_id, step_key, scope_type, scope_id,
                 input_fingerprint),
            )
            return dict(self._get(conn, "run_steps", identifier))

    def get_step(self, step_id: str) -> dict[str, Any]:
        with repository_read(self.database_path) as conn:
            return dict(self._get(conn, "run_steps", step_id))

    def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        with repository_read(self.database_path) as conn:
            self._get(conn, "runs", run_id)
            return [
                dict(r) for r in conn.execute(
                    """SELECT * FROM run_steps WHERE run_id = ?
                       ORDER BY rowid""", (run_id,)
                ).fetchall()
            ]

    def mark_step_stale(
        self, step_id: str, *, new_fingerprint: str,
        lease_owner: str | None = None,
    ) -> None:
        _required(new_fingerprint, "new_fingerprint")
        with repository_write(self.database_path) as conn:
            old = self._get(conn, "run_steps", step_id)
            self._assert_owner(conn, str(old["run_id"]), lease_owner)
            self._assert_run_step_history_mutable(conn, str(old["run_id"]))
            if old["status"] != "COMPLETED":
                raise DurableRunStateError("only a completed step can become STALE")
            if old["input_fingerprint"] == new_fingerprint:
                raise DurableRunStateError("matching fingerprint must not stale step")
            conn.execute(
                "UPDATE run_steps SET status = 'STALE' WHERE id = ?",
                (step_id,),
            )

    def start_step(
        self,
        step_id: str,
        *,
        input_fingerprint: str,
        lease_owner: str | None = None,
    ) -> dict[str, Any]:
        _required(input_fingerprint, "input_fingerprint")
        timestamp = self._now().isoformat()
        with repository_write(self.database_path) as conn:
            old = self._get(conn, "run_steps", step_id)
            self._assert_owner(conn, str(old["run_id"]), lease_owner)
            run = self._get(conn, "runs", str(old["run_id"]))
            if run["status"] != "RUNNING":
                raise DurableRunStateError("cannot start step unless Run is RUNNING")
            if old["status"] not in _STEP_STARTABLE:
                raise DurableRunStateError(
                    f"step cannot start from {old['status']}"
                )
            if old["status"] == "COMPLETED":
                raise DurableRunStateError("completed step cannot restart")
            if old["status"] not in {
                "STALE", "FAILED_RETRYABLE", "INTERRUPTED",
                "NEEDS_ATTENTION",
            } and old["input_fingerprint"] != input_fingerprint:
                raise DurableRunStateError(
                    "new fingerprint requires explicit invalidation"
                )
            conn.execute(
                """
                UPDATE run_steps SET status = 'RUNNING',
                    attempt_count = attempt_count + 1,
                    input_fingerprint = ?, output_json = '{}',
                    error_json = '{}', started_at = ?, finished_at = NULL,
                    heartbeat_at = ?
                WHERE id = ?
                """,
                (input_fingerprint, timestamp, timestamp, step_id),
            )
            step = self._get(conn, "run_steps", step_id)
            conn.execute(
                """INSERT INTO run_step_attempts (
                    id, run_step_id, attempt_no, input_fingerprint,
                    status, output_json, error_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, 'RUNNING', '{}', '{}', ?, NULL)""",
                (new_id("attempt"), step_id, int(step["attempt_count"]),
                 input_fingerprint, timestamp),
            )
            return dict(step)

    def heartbeat_step(self, step_id: str, *, lease_owner: str) -> None:
        """Heartbeat only the active attempt held by the current Work owner.

        Verify the Run/lease under the same write transaction that updates
        the Step: an old executor cannot touch a newer resumed attempt.
        """
        _required(lease_owner, "lease_owner")
        with repository_write(self.database_path) as conn:
            step = self._get(conn, "run_steps", step_id)
            self._assert_owner(conn, str(step["run_id"]), lease_owner)
            self._assert_run_step_history_mutable(conn, str(step["run_id"]))
            cursor = conn.execute(
                """UPDATE run_steps SET heartbeat_at = ?
                   WHERE id = ? AND status = 'RUNNING'""",
                (self._now().isoformat(), step_id),
            )
            if cursor.rowcount != 1:
                raise DurableRunStateError("step is not RUNNING")

    def finish_step(
        self,
        step_id: str,
        *,
        status: str,
        output: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        lease_owner: str | None = None,
    ) -> dict[str, Any]:
        if status not in _STEP_TERMINAL:
            raise DurableRunStateError(f"invalid step completion status {status}")
        output_json = canonical_json(dict(output or {}))
        error_json = canonical_json(dict(error or {}))
        with repository_write(self.database_path) as conn:
            old = self._get(conn, "run_steps", step_id)
            self._assert_owner(conn, str(old["run_id"]), lease_owner)
            self._assert_run_step_history_mutable(conn, str(old["run_id"]))
            if old["status"] != "RUNNING":
                raise DurableRunStateError("only a RUNNING step may finish")
            finished = self._now().isoformat()
            conn.execute(
                """UPDATE run_steps SET status = ?, output_json = ?,
                    error_json = ?, finished_at = ? WHERE id = ?""",
                (status, output_json, error_json, finished, step_id),
            )
            cursor = conn.execute(
                """UPDATE run_step_attempts SET status = ?, output_json = ?,
                    error_json = ?, finished_at = ?
                    WHERE run_step_id = ? AND attempt_no = ?
                      AND status = 'RUNNING'""",
                (status, output_json, error_json, finished, step_id,
                 int(old["attempt_count"])),
            )
            if cursor.rowcount != 1:
                raise DurableRunStateError("current step attempt record is missing")
            return dict(self._get(conn, "run_steps", step_id))

    def _assert_work(self, conn: sqlite3.Connection, work_id: str) -> None:
        row = conn.execute(
            "SELECT work_id FROM work_metadata WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        if row is None:
            raise DurableRunNotFoundError(f"Work {work_id!r} not found")

    def inspect_lease(self, work_id: str) -> dict[str, Any] | None:
        with repository_read(self.database_path) as conn:
            self._assert_work(conn, work_id)
            lease = _row(conn.execute(
                "SELECT * FROM work_leases WHERE work_id = ?",
                (work_id,),
            ).fetchone())
        if lease is not None:
            lease["expired"] = (
                datetime.fromisoformat(str(lease["expires_at"])) <= self._now()
            )
        return lease

    def acquire_lease(
        self,
        *,
        work_id: str,
        lease_owner: str,
        lease_kind: str,
        ttl_seconds: int,
        run_id: str | None = None,
        reclaim_expired_owner: str | None = None,
    ) -> dict[str, Any]:
        _required(work_id, "work_id")
        _required(lease_owner, "lease_owner")
        _required(lease_kind, "lease_kind")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        with repository_write(self.database_path) as conn:
            # This transaction may wait on another SQLite writer. Sample now
            # only after BEGIN IMMEDIATE has acquired the lock, so a lease
            # that expires while waiting cannot be classified as still live.
            now = self._now()
            self._assert_work(conn, work_id)
            if run_id is not None:
                run = self._get(conn, "runs", run_id)
                if run["scope_type"] != "WORK" or run["scope_id"] != work_id:
                    raise WorkLeaseConflictError("Run belongs to a different Work")
            if reclaim_expired_owner == lease_owner:
                raise WorkLeaseConflictError(
                    "recovery must rotate the unique lease owner token"
                )
            old = conn.execute(
                "SELECT * FROM work_leases WHERE work_id = ?",
                (work_id,),
            ).fetchone()
            # A non-null Run owner is a durable crashed-worker marker, but
            # it must not become a recovery capability on its own. Verify
            # that the *previous* lease truly belonged to this Run/owner and
            # was explicitly presented for rotation before replacing it.
            # This also fences legacy orphan Run-owner snapshots.
            if run_id is not None and run["status"] == "RUNNING" and (
                run["lease_owner"] is not None
            ):
                if (
                    old is None
                    or old["run_id"] != run_id
                    or old["lease_owner"] != run["lease_owner"]
                    or reclaim_expired_owner != run["lease_owner"]
                ):
                    raise WorkLeaseConflictError(
                        "RUNNING Run requires explicit recovery of its "
                        "prior Run-bound lease owner"
                    )
            if old is not None:
                expiry = _utc(datetime.fromisoformat(old["expires_at"]))
                if expiry > now:
                    raise WorkLeaseConflictError(
                        f"Work {work_id!r} has an active mutation lease"
                    )
                if (
                    reclaim_expired_owner is None
                    or old["lease_owner"] != reclaim_expired_owner
                ):
                    raise WorkLeaseConflictError(
                        "expired lease requires explicit matching-owner recovery"
                    )
                # Cross-Run recovery cannot abandon an old RUNNING Run or
                # any in-flight Step/Attempt. Reclaim the same Run first,
                # explicitly reconcile it, release its lease, then start the
                # next Run. This check and the lease transfer are atomic.
                if old["run_id"] is not None and old["run_id"] != run_id:
                    previous_id = str(old["run_id"])
                    previous = self._get(conn, "runs", previous_id)
                    if (
                        previous["scope_type"] != "WORK"
                        or previous["scope_id"] != work_id
                    ):
                        raise WorkLeaseConflictError(
                            "previous lease Run belongs to a different Work"
                        )
                    if previous["status"] == "RUNNING":
                        raise WorkLeaseConflictError(
                            "previous RUNNING Run requires explicit reconciliation "
                            "before cross-Run lease transfer"
                        )
                    running_step = conn.execute(
                        "SELECT 1 FROM run_steps "
                        "WHERE run_id = ? AND status = 'RUNNING' LIMIT 1",
                        (previous_id,),
                    ).fetchone()
                    running_attempt = conn.execute(
                        "SELECT 1 FROM run_step_attempts "
                        "WHERE status = 'RUNNING' AND run_step_id IN "
                        "(SELECT id FROM run_steps WHERE run_id = ?) LIMIT 1",
                        (previous_id,),
                    ).fetchone()
                    if running_step is not None or running_attempt is not None:
                        raise WorkLeaseConflictError(
                            "previous Run has unreconciled RUNNING "
                            "Step or Attempt receipts"
                        )
                conn.execute("DELETE FROM work_leases WHERE work_id = ?", (work_id,))
            elif reclaim_expired_owner is not None:
                raise WorkLeaseConflictError("no matching expired lease to recover")
            conn.execute(
                """
                INSERT INTO work_leases (
                    work_id, lease_owner, lease_kind, run_id,
                    heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (work_id, lease_owner, lease_kind, run_id, now.isoformat(),
                 (now + timedelta(seconds=ttl_seconds)).isoformat()),
            )
            return dict(conn.execute(
                "SELECT * FROM work_leases WHERE work_id = ?", (work_id,)
            ).fetchone())

    def heartbeat_lease(
        self, *, work_id: str, lease_owner: str, ttl_seconds: int,
    ) -> dict[str, Any]:
        _required(lease_owner, "lease_owner")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        with repository_write(self.database_path) as conn:
            # An expired owner must not renew just because this call started
            # before another SQLite writer released BEGIN IMMEDIATE.
            now = self._now()
            old = conn.execute(
                "SELECT * FROM work_leases WHERE work_id = ?", (work_id,),
            ).fetchone()
            if (
                old is None
                or old["lease_owner"] != lease_owner
                or _utc(datetime.fromisoformat(old["expires_at"])) <= now
            ):
                raise WorkLeaseConflictError("mutation lease is missing, lost or expired")
            conn.execute(
                """UPDATE work_leases SET heartbeat_at = ?, expires_at = ?
                   WHERE work_id = ? AND lease_owner = ?""",
                (now.isoformat(),
                 (now + timedelta(seconds=ttl_seconds)).isoformat(),
                 work_id, lease_owner),
            )
            return dict(conn.execute(
                "SELECT * FROM work_leases WHERE work_id = ?", (work_id,)
            ).fetchone())

    def release_lease(self, *, work_id: str, lease_owner: str) -> None:
        _required(lease_owner, "lease_owner")
        with repository_write(self.database_path) as conn:
            lease = conn.execute(
                "SELECT expires_at, run_id FROM work_leases "
                "WHERE work_id = ? AND lease_owner = ?",
                (work_id, lease_owner),
            ).fetchone()
            if (
                lease is None
                or _utc(datetime.fromisoformat(lease["expires_at"])) <= self._now()
            ):
                # An expired owner is no longer authorized to erase its
                # recovery evidence. Keep the tombstone for explicit,
                # matching-token reclaim by a different owner.
                raise WorkLeaseConflictError(
                    "mutation lease missing, expired or owner mismatch"
                )
            # A normal release is not a recovery operation. Never erase the
            # only explicit old-owner recovery token while the associated
            # durable Run or any child receipt still reports live execution.
            # Validate and delete under this same BEGIN IMMEDIATE transaction,
            # including a terminal Run with an inconsistent RUNNING Attempt.
            if lease["run_id"] is not None:
                attached_run = conn.execute(
                    "SELECT status, scope_type, scope_id FROM runs WHERE id = ?",
                    (lease["run_id"],),
                ).fetchone()
                if (
                    attached_run is None
                    or attached_run["scope_type"] != "WORK"
                    or attached_run["scope_id"] != work_id
                ):
                    raise WorkLeaseConflictError(
                        "lease is attached to a missing or foreign Work Run"
                    )
                if attached_run["status"] == "RUNNING":
                    raise WorkLeaseConflictError(
                        "cannot release Work lease while its Run is RUNNING"
                    )
                active_step = conn.execute(
                    "SELECT 1 FROM run_steps "
                    "WHERE run_id = ? AND status = 'RUNNING' LIMIT 1",
                    (lease["run_id"],),
                ).fetchone()
                active_attempt = conn.execute(
                    "SELECT 1 FROM run_step_attempts "
                    "WHERE status = 'RUNNING' AND run_step_id IN "
                    "(SELECT id FROM run_steps WHERE run_id = ?) LIMIT 1",
                    (lease["run_id"],),
                ).fetchone()
                if active_step is not None or active_attempt is not None:
                    raise WorkLeaseConflictError(
                        "cannot release Work lease with RUNNING Step or Attempt"
                    )
            conn.execute(
                "DELETE FROM work_leases WHERE work_id = ? AND lease_owner = ?",
                (work_id, lease_owner),
            )
