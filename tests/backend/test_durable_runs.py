"""Durable Run/RunStep/lease persistence integration tests (#245)."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from manga_autopilot.repositories.durable_runs import (
    DurableRunNotFoundError,
    DurableRunRepository,
    DurableRunStateError,
    WorkLeaseConflictError,
)
from manga_autopilot.storage import (
    WORK_MIGRATIONS,
    bootstrap_work_database,
    create_work_commit,
    create_work_entity_revision,
    repository_read,
    repository_write,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 9, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _work(db: Path, *, migrations=WORK_MIGRATIONS) -> None:
    bootstrap_work_database(db, work_id="work_test", migrations=migrations)
    with repository_write(db) as conn:
        c = create_work_commit(
            conn, commit_id="commit_seed", actor_type="system",
            operation_type="create_work",
        )
        conn.execute(
            """INSERT INTO work_metadata (
                work_id, title, work_kind, language, reading_direction,
                status, current_commit_seq, current_revision,
                created_at, updated_at
            ) VALUES ('work_test', 'Test', 'standalone', 'ja',
                'RTL_TOP_TO_BOTTOM', 'DRAFT', ?, 1, ?, ?)""",
            (c.commit_seq, c.created_at, c.created_at),
        )
        create_work_entity_revision(
            conn, revision_id="revision_seed", entity_type="work",
            entity_id="work_test", entity_revision=1, commit_seq=c.commit_seq,
            change_kind="create", after_state={"title": "Test"},
        )


def _run(repo: DurableRunRepository) -> str:
    row = repo.create_run(
        run_kind="AUTOPILOT", scope_type="WORK", scope_id="work_test",
        requested_by="user", input_fingerprint="input:v1",
    )
    return str(row["id"])


def test_migration_creates_exact_durable_tables_and_indices(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    assert WORK_MIGRATIONS[-1].version == 7
    with repository_read(db) as conn:
        for name in ("runs", "run_steps", "work_leases", "run_step_attempts"):
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'uq_run_steps_nullable_scope'"
        ).fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_existing_work_v5_upgrades_without_losing_history(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db, migrations=WORK_MIGRATIONS[:5])
    before = db.stat().st_size
    result = bootstrap_work_database(db, work_id="work_test")
    assert result.migration.applied_versions == (6, 7)
    assert result.migration.backup_path is not None
    assert result.migration.backup_path.exists()
    assert before > 0
    with repository_read(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM work_metadata"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_revisions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT name FROM schema_migrations WHERE version = 6"
        ).fetchone()[0] == "W0006_durable_runs_and_work_leases"


def test_run_and_step_survive_reinstantiation_and_keep_fingerprint(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="worker_a",
        lease_kind="MUTATION", ttl_seconds=60, run_id=run_id,
    )
    repo.transition_run(run_id, expected_status="PENDING",
                        new_status="RUNNING", lease_owner="worker_a")
    step = repo.create_step(
        run_id=run_id, step_key="PREFLIGHT", input_fingerprint="preflight:v1",
    )
    step_id = str(step["id"])
    started = repo.start_step(step_id, input_fingerprint="preflight:v1")
    assert started["attempt_count"] == 1
    finished = repo.finish_step(step_id, status="COMPLETED",
                                output={"passed": True})
    assert finished["input_fingerprint"] == "preflight:v1"
    assert finished["finished_at"]
    fresh = DurableRunRepository(db, clock=clock)
    assert fresh.get_run(run_id)["status"] == "RUNNING"
    assert fresh.get_step(step_id)["status"] == "COMPLETED"
    assert fresh.get_step(step_id)["output_json"] == '{"passed":true}'
    assert len(fresh.list_steps(run_id)) == 1
    with pytest.raises(DurableRunStateError):
        fresh.start_step(step_id, input_fingerprint="preflight:v1")
    fresh.mark_step_stale(step_id, new_fingerprint="preflight:v2")
    restarted = fresh.start_step(step_id, input_fingerprint="preflight:v2")
    assert restarted["attempt_count"] == 2
    assert restarted["output_json"] == "{}"
    assert restarted["input_fingerprint"] == "preflight:v2"


def test_step_retry_and_status_guards(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    step_id = repo.create_step(
        run_id=run_id, step_key="generate", input_fingerprint="v1",
    )["id"]
    with pytest.raises(DurableRunStateError, match="Run is RUNNING"):
        repo.start_step(step_id, input_fingerprint="v1")
    repo.transition_run(run_id, expected_status="PENDING",
                        new_status="RUNNING")
    with pytest.raises(DurableRunStateError, match="invalidation"):
        repo.start_step(step_id, input_fingerprint="different")
    repo.start_step(step_id, input_fingerprint="v1")
    repo.finish_step(step_id, status="FAILED_RETRYABLE",
                     error={"reason": "temporary"})
    retry = repo.start_step(step_id, input_fingerprint="v1")
    assert retry["attempt_count"] == 2
    repo.finish_step(step_id, status="FAILED_TERMINAL")
    with pytest.raises(DurableRunStateError):
        repo.start_step(step_id, input_fingerprint="v1")
    with pytest.raises(DurableRunStateError):
        repo.transition_run(run_id, expected_status="PENDING",
                            new_status="RUNNING")
    repo.transition_run(run_id, expected_status="RUNNING",
                        new_status="FAILED_TERMINAL")
    with pytest.raises(DurableRunStateError):
        repo.transition_run(run_id, expected_status="FAILED_TERMINAL",
                            new_status="RUNNING")


def test_nullable_step_scope_is_unique_and_fk_is_enforced(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.create_step(run_id=run_id, step_key="SAME",
                     input_fingerprint="v1")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_step(run_id=run_id, step_key="SAME",
                         input_fingerprint="v1")
    repo.create_step(run_id=run_id, step_key="SAME",
                     scope_type="PAGE", scope_id="page1", input_fingerprint="v1")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_step(run_id=run_id, step_key="SAME",
                         scope_type="PAGE", scope_id="page1",
                         input_fingerprint="v1")
    with pytest.raises(ValueError, match="both"):
        repo.create_step(run_id=run_id, step_key="OTHER",
                         scope_type="PAGE", input_fingerprint="v1")
    with pytest.raises(DurableRunNotFoundError):
        repo.create_step(run_id="run_missing", step_key="OTHER",
                         input_fingerprint="v1")


def test_lease_conflict_expiry_and_explicit_owner_recovery(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    lease = repo.acquire_lease(
        work_id="work_test", lease_owner="owner_a", lease_kind="MUTATION",
        ttl_seconds=10, run_id=run_id,
    )
    assert lease["run_id"] == run_id
    with pytest.raises(WorkLeaseConflictError, match="active"):
        repo.acquire_lease(
            work_id="work_test", lease_owner="owner_b", lease_kind="MUTATION",
            ttl_seconds=10,
        )
    clock.advance(9)
    extended = repo.heartbeat_lease(
        work_id="work_test", lease_owner="owner_a", ttl_seconds=20,
    )
    assert extended["expires_at"] > lease["expires_at"]
    clock.advance(21)
    assert repo.inspect_lease("work_test")["expired"] is True
    with pytest.raises(WorkLeaseConflictError, match="rotate"):
        repo.acquire_lease(
            work_id="work_test", lease_owner="owner_a", lease_kind="MUTATION",
            ttl_seconds=10, reclaim_expired_owner="owner_a",
        )
    with pytest.raises(WorkLeaseConflictError, match="explicit"):
        repo.acquire_lease(
            work_id="work_test", lease_owner="owner_b", lease_kind="MUTATION",
            ttl_seconds=10,
        )
    with pytest.raises(WorkLeaseConflictError):
        repo.acquire_lease(
            work_id="work_test", lease_owner="owner_b", lease_kind="MUTATION",
            ttl_seconds=10, reclaim_expired_owner="not_owner_a",
        )
    recovered = repo.acquire_lease(
        work_id="work_test", lease_owner="owner_b", lease_kind="MUTATION",
        ttl_seconds=10, reclaim_expired_owner="owner_a",
    )
    assert recovered["lease_owner"] == "owner_b"
    with pytest.raises(WorkLeaseConflictError):
        repo.release_lease(work_id="work_test", lease_owner="owner_a")
    with pytest.raises(WorkLeaseConflictError):
        repo.heartbeat_lease(work_id="work_test", lease_owner="owner_a",
                             ttl_seconds=10)
    repo.release_lease(work_id="work_test", lease_owner="owner_b")
    assert repo.inspect_lease("work_test") is None


def test_expired_owner_cannot_erase_explicit_recovery_token(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    original = DurableRunRepository(db, clock=clock)
    another = DurableRunRepository(db, clock=clock)
    run_id = _run(original)
    original.acquire_lease(
        work_id="work_test", lease_owner="stale",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    clock.advance(4)
    with pytest.raises(WorkLeaseConflictError, match="expired"):
        original.release_lease(work_id="work_test", lease_owner="stale")
    lease = another.inspect_lease("work_test")
    assert lease is not None and lease["expired"]
    assert lease["lease_owner"] == "stale"
    with pytest.raises(WorkLeaseConflictError, match="explicit"):
        another.acquire_lease(
            work_id="work_test", lease_owner="fresh",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=run_id,
        )
    restored = another.acquire_lease(
        work_id="work_test", lease_owner="fresh",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=run_id,
        reclaim_expired_owner="stale",
    )
    assert restored["lease_owner"] == "fresh"
    with pytest.raises(WorkLeaseConflictError):
        original.release_lease(work_id="work_test", lease_owner="stale")
    another.release_lease(work_id="work_test", lease_owner="fresh")
    assert another.inspect_lease("work_test") is None


def test_two_connections_can_never_acquire_same_work_lease(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()

    def attempt(owner: str) -> str:
        repo = DurableRunRepository(db, clock=clock)
        try:
            repo.acquire_lease(
                work_id="work_test", lease_owner=owner,
                lease_kind="MUTATION", ttl_seconds=60,
            )
            return "acquired"
        except WorkLeaseConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("owner_a", "owner_b")))
    assert sorted(outcomes) == ["acquired", "conflict"]


def test_lease_requires_existing_work_and_run(tmp_path: Path) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    with pytest.raises(DurableRunNotFoundError):
        repo.acquire_lease(work_id="work_missing", lease_owner="owner",
                           lease_kind="MUTATION", ttl_seconds=1)
    with pytest.raises(DurableRunNotFoundError):
        repo.acquire_lease(work_id="work_test", lease_owner="owner",
                           lease_kind="MUTATION", ttl_seconds=1,
                           run_id="run_missing")
    run_id = repo.create_run(
        run_kind="AUTOPILOT", scope_type="WORK", scope_id="other_work",
        requested_by="user", input_fingerprint="v1",
    )["id"]
    with pytest.raises(WorkLeaseConflictError, match="different Work"):
        repo.acquire_lease(work_id="work_test", lease_owner="owner",
                           lease_kind="MUTATION", ttl_seconds=1,
                           run_id=run_id)
    assert repo.inspect_lease("work_test") is None
