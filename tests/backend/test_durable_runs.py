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
    assert WORK_MIGRATIONS[-1].version == 8
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
    assert result.migration.applied_versions == (6, 7, 8)
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
        lease_owner="worker_a",
    )
    step_id = str(step["id"])
    started = repo.start_step(
        step_id, input_fingerprint="preflight:v1", lease_owner="worker_a",
    )
    assert started["attempt_count"] == 1
    finished = repo.finish_step(
        step_id, status="COMPLETED", output={"passed": True},
        lease_owner="worker_a",
    )
    assert finished["input_fingerprint"] == "preflight:v1"
    assert finished["finished_at"]
    fresh = DurableRunRepository(db, clock=clock)
    assert fresh.get_run(run_id)["status"] == "RUNNING"
    assert fresh.get_step(step_id)["status"] == "COMPLETED"
    assert fresh.get_step(step_id)["output_json"] == '{"passed":true}'
    assert len(fresh.list_steps(run_id)) == 1
    with pytest.raises(DurableRunStateError):
        fresh.start_step(
            step_id, input_fingerprint="preflight:v1", lease_owner="worker_a",
        )
    fresh.mark_step_stale(
        step_id, new_fingerprint="preflight:v2", lease_owner="worker_a",
    )
    restarted = fresh.start_step(
        step_id, input_fingerprint="preflight:v2", lease_owner="worker_a",
    )
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



def test_step_heartbeat_fences_stale_owner_after_explicit_reclaim(
    tmp_path: Path,
) -> None:
    """An old worker cannot impersonate the active step's new attempt."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    original = DurableRunRepository(db, clock=clock)
    fresh = DurableRunRepository(db, clock=clock)
    run_id = _run(original)
    original.acquire_lease(
        work_id="work_test", lease_owner="first",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=6, run_id=run_id,
    )
    original.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="first",
    )
    step = original.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="first",
    )
    step_id = step["id"]
    original.start_step(
        step_id, input_fingerprint="v1", lease_owner="first",
    )
    initial = fresh.get_step(step_id)["heartbeat_at"]
    clock.advance(1)
    original.heartbeat_step(step_id, lease_owner="first")
    assert fresh.get_step(step_id)["heartbeat_at"] > initial

    clock.advance(7)
    assert fresh.inspect_lease("work_test")["expired"] is True
    fresh.acquire_lease(
        work_id="work_test", lease_owner="second",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=12,
        run_id=run_id, reclaim_expired_owner="first",
    )
    fresh.recover_interrupted_run(run_id, lease_owner="second")
    fresh.transition_run(
        run_id, expected_status="INTERRUPTED", new_status="RUNNING",
        lease_owner="second",
    )
    fresh.start_step(
        step_id, input_fingerprint="v1", lease_owner="second",
    )
    before = fresh.get_step(step_id)
    assert before["attempt_count"] == 2
    receipts = fresh.list_step_attempts(step_id)
    assert [a["status"] for a in receipts] == ["INTERRUPTED", "RUNNING"]
    clock.advance(1)

    with pytest.raises(WorkLeaseConflictError):
        original.heartbeat_step(step_id, lease_owner="first")
    assert fresh.get_step(step_id)["heartbeat_at"] == before["heartbeat_at"]
    assert fresh.list_step_attempts(step_id) == receipts
    # Even callers with a step ID but no owner token cannot write heartbeat.
    with pytest.raises(TypeError):
        original.heartbeat_step(step_id)

    fresh.heartbeat_step(step_id, lease_owner="second")
    assert fresh.get_step(step_id)["heartbeat_at"] > before["heartbeat_at"]
    assert fresh.list_step_attempts(step_id) == receipts

    fresh.finish_step(
        step_id, status="COMPLETED", output={"value": True},
        lease_owner="second",
    )
    finished = fresh.get_step(step_id)
    clock.advance(1)
    with pytest.raises(DurableRunStateError, match="RUNNING"):
        fresh.heartbeat_step(step_id, lease_owner="second")
    assert fresh.get_step(step_id)["heartbeat_at"] == finished["heartbeat_at"]
    assert [a["status"] for a in fresh.list_step_attempts(step_id)] == [
        "INTERRUPTED", "COMPLETED",
    ]


@pytest.mark.parametrize("mutation", ["step_receipt", "run_terminal"])
def test_leased_durable_run_rejects_ownerless_receipt_mutation(
    tmp_path: Path, mutation: str,
) -> None:
    """Issue #378: a second Repository cannot forge the active owner's receipt."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    owner = DurableRunRepository(db)
    intruder = DurableRunRepository(db)
    run_id = _run(owner)
    owner.acquire_lease(
        work_id="work_test", lease_owner="active_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    owner.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="active_owner",
    )
    step = owner.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="good", lease_owner="active_owner",
    )
    owner.start_step(
        step["id"], input_fingerprint="good", lease_owner="active_owner",
    )
    before_run = owner.get_run(run_id)
    before_step = owner.get_step(step["id"])
    before_attempts = owner.list_step_attempts(step["id"])
    if mutation == "step_receipt":
        with pytest.raises(WorkLeaseConflictError):
            intruder.finish_step(
                step["id"], status="COMPLETED",
                output={"value": "forged"},
            )
    else:
        with pytest.raises(WorkLeaseConflictError):
            intruder.transition_run(
                run_id, expected_status="RUNNING", new_status="COMPLETED",
            )
    assert owner.get_run(run_id) == before_run
    assert owner.get_step(step["id"]) == before_step
    assert owner.list_step_attempts(step["id"]) == before_attempts
    assert owner.inspect_lease("work_test")["lease_owner"] == "active_owner"
    owner.finish_step(
        step["id"], status="COMPLETED", output={"value": "owned"},
        lease_owner="active_owner",
    )
    owner.transition_run(
        run_id, expected_status="RUNNING", new_status="COMPLETED",
        lease_owner="active_owner",
    )


@pytest.mark.parametrize("step_action", [
    "create_step", "set_fingerprint", "start_step", "finish_step",
    "mark_stale",
])
@pytest.mark.parametrize("untrusted_owner", [None, "wrong_owner"])
def test_leased_step_mutations_all_require_current_owner(
    tmp_path: Path, step_action: str, untrusted_owner: str | None,
) -> None:
    """Fence every mutating durable RunStep operation, not just finish_step."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    owner = DurableRunRepository(db)
    intruder = DurableRunRepository(db)
    run_id = _run(owner)
    owner.acquire_lease(
        work_id="work_test", lease_owner="active_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    owner.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="active_owner",
    )
    step = owner.create_step(
        run_id=run_id, step_key="original",
        input_fingerprint="v1", lease_owner="active_owner",
    )
    if step_action in {"finish_step", "mark_stale"}:
        owner.start_step(
            step["id"], input_fingerprint="v1", lease_owner="active_owner",
        )
    if step_action == "mark_stale":
        owner.finish_step(
            step["id"], status="COMPLETED", lease_owner="active_owner",
        )
    before_run = owner.get_run(run_id)
    before_step = owner.get_step(step["id"])
    before_attempts = owner.list_step_attempts(step["id"])
    before_steps = owner.list_steps(run_id)
    with pytest.raises(WorkLeaseConflictError):
        if step_action == "create_step":
            intruder.create_step(
                run_id=run_id, step_key="forged", input_fingerprint="v1",
                lease_owner=untrusted_owner,
            )
        elif step_action == "set_fingerprint":
            intruder.set_pending_fingerprint(
                step["id"], input_fingerprint="forged",
                lease_owner=untrusted_owner,
            )
        elif step_action == "start_step":
            intruder.start_step(
                step["id"], input_fingerprint="v1",
                lease_owner=untrusted_owner,
            )
        elif step_action == "finish_step":
            intruder.finish_step(
                step["id"], status="COMPLETED",
                output={"value": "forged"}, lease_owner=untrusted_owner,
            )
        else:
            intruder.mark_step_stale(
                step["id"], new_fingerprint="v2",
                lease_owner=untrusted_owner,
            )
    assert owner.get_run(run_id) == before_run
    assert owner.get_step(step["id"]) == before_step
    assert owner.list_step_attempts(step["id"]) == before_attempts
    assert owner.list_steps(run_id) == before_steps


@pytest.mark.parametrize("owner_token", [None, "foreign_owner"])
def test_leased_run_rejects_foreign_state_change_and_run_creation(
    tmp_path: Path, owner_token: str | None,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    pending_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="active_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    original_pending = repo.get_run(pending_id)
    with pytest.raises(WorkLeaseConflictError):
        repo.transition_run(
            pending_id, expected_status="PENDING", new_status="CANCELLED",
            lease_owner=owner_token,
        )
    assert repo.get_run(pending_id) == original_pending
    with pytest.raises(WorkLeaseConflictError):
        repo.create_run(
            run_kind="EXPORT", scope_type="WORK", scope_id="work_test",
            requested_by="foreign", input_fingerprint="v1",
        )
    repo.release_lease(work_id="work_test", lease_owner="active_owner")
    assert repo.get_run(pending_id) == original_pending
    # Outside an active Work lease, generic non-AUTOPILOT run creation is valid.
    assert repo.create_run(
        run_kind="EXPORT", scope_type="WORK", scope_id="work_test",
        requested_by="user", input_fingerprint="v1",
    )["run_kind"] == "EXPORT"


def test_ownerless_durable_mutations_refuse_expired_lease_tombstone(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    step = repo.create_step(
        run_id=run_id, step_key="preflight", input_fingerprint="v1",
    )
    repo.acquire_lease(
        work_id="work_test", lease_owner="expired_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    clock.advance(4)
    with pytest.raises(WorkLeaseConflictError):
        repo.set_pending_fingerprint(
            step["id"], input_fingerprint="new",
        )
    with pytest.raises(WorkLeaseConflictError):
        repo.transition_run(
            run_id, expected_status="PENDING", new_status="RUNNING",
        )
    assert repo.get_run(run_id)["status"] == "PENDING"
    assert repo.get_step(step["id"])["input_fingerprint"] == "v1"
    assert repo.inspect_lease("work_test")["expired"] is True



# #379: audit RED cases retained as regression tests. A cross-Run reclaim
# must never abandon a RUNNING Run/Step/Attempt, even with explicit old token.
@pytest.mark.parametrize("old_step_state", [
    "no_steps", "completed_step", "running_attempt",
])
@pytest.mark.parametrize("next_run", ["different", "unbound"])
def test_cross_run_expired_lease_rejects_unreconciled_running_run(
    tmp_path: Path, old_step_state: str, next_run: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    old_run = _run(repo)
    new_run = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="crashed_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=old_run,
    )
    repo.transition_run(
        old_run, expected_status="PENDING", new_status="RUNNING",
        lease_owner="crashed_owner",
    )
    step_id = None
    if old_step_state != "no_steps":
        step_id = repo.create_step(
            run_id=old_run, step_key="generate_panels",
            input_fingerprint="input:v1", lease_owner="crashed_owner",
        )["id"]
        repo.start_step(
            step_id, input_fingerprint="input:v1",
            lease_owner="crashed_owner",
        )
        if old_step_state == "completed_step":
            repo.finish_step(
                step_id, status="COMPLETED",
                output={"value": "already_persisted"},
                lease_owner="crashed_owner",
            )
    original_run = repo.get_run(old_run)
    original_steps = repo.list_steps(old_run)
    original_attempts = repo.list_step_attempts(step_id) if step_id else []
    clock.advance(11)
    tombstone = repo.inspect_lease("work_test")
    assert tombstone is not None and tombstone["expired"]
    restarted = DurableRunRepository(db, clock=clock)
    for token in ("wrong_old_owner", "crashed_owner", "crashed_owner"):
        with pytest.raises(WorkLeaseConflictError):
            restarted.acquire_lease(
                work_id="work_test", lease_owner="new_owner",
                lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
                run_id=new_run if next_run == "different" else None,
                reclaim_expired_owner=token,
            )
        assert restarted.get_run(old_run) == original_run
        assert restarted.list_steps(old_run) == original_steps
        if step_id:
            assert restarted.list_step_attempts(step_id) == original_attempts
        assert restarted.inspect_lease("work_test") == tombstone
    assert restarted.get_run(new_run)["status"] == "PENDING"

    # Explicit same-Run recovery fences the crash before work can move.
    lease = restarted.acquire_lease(
        work_id="work_test", lease_owner="recovery_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=old_run,
        reclaim_expired_owner="crashed_owner",
    )
    assert lease["run_id"] == old_run
    restarted.recover_interrupted_run(old_run, lease_owner="recovery_owner")
    assert restarted.get_run(old_run)["status"] == "INTERRUPTED"
    if step_id:
        expected = ("COMPLETED" if old_step_state == "completed_step"
                    else "INTERRUPTED")
        assert restarted.get_step(step_id)["status"] == expected
        assert restarted.list_step_attempts(step_id)[0]["status"] == expected
    restarted.release_lease(
        work_id="work_test", lease_owner="recovery_owner",
    )
    moved = restarted.acquire_lease(
        work_id="work_test", lease_owner="new_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=new_run,
    )
    assert moved["run_id"] == new_run
    assert restarted.get_run(old_run)["status"] == "INTERRUPTED"


@pytest.mark.parametrize("old_state", [
    "completed_run", "completed_run_with_active_step",
    "completed_run_with_active_attempt",
])
def test_cross_run_reclaim_requires_reconciled_step_and_attempt_receipts(
    tmp_path: Path, old_state: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    old_run = _run(repo)
    new_run = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="old",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=4, run_id=old_run,
    )
    repo.transition_run(
        old_run, expected_status="PENDING", new_status="RUNNING",
        lease_owner="old",
    )
    step_id = repo.create_step(
        run_id=old_run, step_key="export",
        input_fingerprint="v1", lease_owner="old",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1", lease_owner="old")
    if old_state == "completed_run_with_active_step":
        # Simulate a legacy inconsistent snapshot directly. Production
        # transition_run now correctly rejects this malformed Run/Step pair.
        with repository_write(db) as conn:
            conn.execute(
                "UPDATE runs SET status = 'COMPLETED', lease_owner = NULL "
                "WHERE id = ?", (old_run,),
            )
    else:
        repo.finish_step(
            step_id, status="COMPLETED", lease_owner="old",
        )
        repo.transition_run(
            old_run, expected_status="RUNNING", new_status="COMPLETED",
            lease_owner="old",
        )
    if old_state == "completed_run_with_active_attempt":
        # Simulate an independently malformed legacy receipt: the Run
        # and Step look completed, but its attempt still says RUNNING.
        with repository_write(db) as conn:
            conn.execute(
                "UPDATE run_step_attempts SET status = 'RUNNING' "
                "WHERE run_step_id = ?", (step_id,),
            )
    clock.advance(5)
    before_run = repo.get_run(old_run)
    before_step = repo.get_step(step_id)
    before_attempt = repo.list_step_attempts(step_id)
    if old_state == "completed_run":
        reclaimed = repo.acquire_lease(
            work_id="work_test", lease_owner="new",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
            run_id=new_run, reclaim_expired_owner="old",
        )
        assert reclaimed["run_id"] == new_run
    else:
        with pytest.raises(WorkLeaseConflictError):
            repo.acquire_lease(
                work_id="work_test", lease_owner="new",
                lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
                run_id=new_run, reclaim_expired_owner="old",
            )
        assert repo.inspect_lease("work_test")["lease_owner"] == "old"
    assert repo.get_run(old_run) == before_run
    assert repo.get_step(step_id) == before_step
    assert repo.list_step_attempts(step_id) == before_attempt


def test_expired_unbound_lease_allows_explicit_reclaim_into_run(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="maintenance",
        lease_kind="MUTATION", ttl_seconds=4,
    )
    clock.advance(5)
    new_lease = repo.acquire_lease(
        work_id="work_test", lease_owner="new",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
        run_id=run_id, reclaim_expired_owner="maintenance",
    )
    assert new_lease["run_id"] == run_id
    assert repo.get_run(run_id)["status"] == "PENDING"


@pytest.mark.parametrize("foreign_scope", [
    ("PAGE", "page_1"), ("WORK", "another_work"),
])
def test_work_mutation_lease_rejects_nonmatching_run_scope(
    tmp_path: Path, foreign_scope: tuple[str, str],
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    foreign = repo.create_run(
        run_kind="AUTOPILOT", scope_type=foreign_scope[0],
        scope_id=foreign_scope[1], requested_by="test",
        input_fingerprint="unrelated",
    )["id"]
    with pytest.raises(WorkLeaseConflictError):
        repo.acquire_lease(
            work_id="work_test", lease_owner="wrong_scope",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
            run_id=foreign,
        )
    assert repo.inspect_lease("work_test") is None


# Regression matrix reproduced in independent Phase C audit for Issue #382.
def test_phase_c_audit_live_lease_release_cannot_orphan_running_run(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    old_run = _run(repo)
    other_run = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="still_running",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60, run_id=old_run,
    )
    repo.transition_run(
        old_run, expected_status="PENDING", new_status="RUNNING",
        lease_owner="still_running",
    )
    step_id = repo.create_step(
        run_id=old_run, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="still_running",
    )["id"]
    repo.start_step(
        step_id, input_fingerprint="v1", lease_owner="still_running",
    )
    before_run = repo.get_run(old_run)
    before_step = repo.get_step(step_id)
    before_attempts = repo.list_step_attempts(step_id)
    old_lease = repo.inspect_lease("work_test")
    with pytest.raises(WorkLeaseConflictError):
        repo.release_lease(work_id="work_test", lease_owner="still_running")
    assert repo.get_run(old_run) == before_run
    assert repo.get_step(step_id) == before_step
    assert repo.list_step_attempts(step_id) == before_attempts
    assert repo.inspect_lease("work_test") == old_lease
    with pytest.raises(WorkLeaseConflictError):
        repo.acquire_lease(
            work_id="work_test", lease_owner="new_owner",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60,
            run_id=other_run,
        )


def test_phase_c_audit_live_lease_release_checks_orphan_step_attempts(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="first",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="first",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="export",
        input_fingerprint="v1", lease_owner="first",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1", lease_owner="first")
    # A historical corrupt parent Run can be created by direct fixture SQL;
    # the public transition_run must not permit creating this state.
    with repository_write(db) as conn:
        conn.execute(
            "UPDATE runs SET status = 'COMPLETED', lease_owner = NULL "
            "WHERE id = ?", (run_id,),
        )
    assert repo.get_run(run_id)["status"] == "COMPLETED"
    assert repo.get_step(step_id)["status"] == "RUNNING"
    attempt_before = repo.list_step_attempts(step_id)
    with pytest.raises(WorkLeaseConflictError):
        repo.release_lease(work_id="work_test", lease_owner="first")
    assert repo.inspect_lease("work_test") is not None
    assert repo.list_step_attempts(step_id) == attempt_before


def test_release_lease_preserves_explicit_recovery_after_running_owner_exits(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    old = DurableRunRepository(db, clock=clock)
    new = DurableRunRepository(db, clock=clock)
    run_id = _run(old)
    next_id = _run(old)
    old.acquire_lease(
        work_id="work_test", lease_owner="abandoned_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=run_id,
    )
    old.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="abandoned_owner",
    )
    step_id = old.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="abandoned_owner",
    )["id"]
    old.start_step(
        step_id, input_fingerprint="v1", lease_owner="abandoned_owner",
    )
    with pytest.raises(WorkLeaseConflictError, match="RUNNING"):
        old.release_lease(work_id="work_test", lease_owner="abandoned_owner")
    assert new.inspect_lease("work_test")["lease_owner"] == "abandoned_owner"
    with pytest.raises(WorkLeaseConflictError):
        new.acquire_lease(
            work_id="work_test", lease_owner="another",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=next_id,
        )
    clock.advance(11)
    with pytest.raises(WorkLeaseConflictError):
        old.release_lease(work_id="work_test", lease_owner="abandoned_owner")
    new.acquire_lease(
        work_id="work_test", lease_owner="recovered_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=run_id,
        reclaim_expired_owner="abandoned_owner",
    )
    new.recover_interrupted_run(run_id, lease_owner="recovered_owner")
    assert new.get_run(run_id)["status"] == "INTERRUPTED"
    assert new.get_step(step_id)["status"] == "INTERRUPTED"
    assert new.list_step_attempts(step_id)[0]["status"] == "INTERRUPTED"
    new.release_lease(work_id="work_test", lease_owner="recovered_owner")
    new.acquire_lease(
        work_id="work_test", lease_owner="next_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=next_id,
    )
    assert new.get_run(next_id)["status"] == "PENDING"


def test_release_lease_rejects_running_attempt_even_if_run_and_step_terminal(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="owner",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="stage", input_fingerprint="v1",
        lease_owner="owner",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1", lease_owner="owner")
    repo.finish_step(step_id, status="COMPLETED", lease_owner="owner")
    repo.transition_run(
        run_id, expected_status="RUNNING", new_status="COMPLETED",
        lease_owner="owner",
    )
    with repository_write(db) as conn:
        conn.execute(
            "UPDATE run_step_attempts SET status = 'RUNNING' "
            "WHERE run_step_id = ?", (step_id,),
        )
    with pytest.raises(WorkLeaseConflictError, match="RUNNING"):
        repo.release_lease(work_id="work_test", lease_owner="owner")
    assert repo.inspect_lease("work_test")["lease_owner"] == "owner"
    assert repo.get_run(run_id)["status"] == "COMPLETED"
    assert repo.get_step(step_id)["status"] == "COMPLETED"
    assert repo.list_step_attempts(step_id)[0]["status"] == "RUNNING"


# Independent Phase C RED regressions for Issue #383.

@pytest.mark.parametrize("transition", [
    "COMPLETED", "FAILED_RETRYABLE", "INTERRUPTED", "CANCELLED",
])
def test_phase_c_audit_run_must_not_exit_running_with_live_step_attempt(
    tmp_path: Path, transition: str,
) -> None:
    """Final Run status cannot conceal a still-RUNNING Step and Attempt."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="active",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="active",
    )
    step = repo.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="active",
    )
    repo.start_step(
        step["id"], input_fingerprint="v1", lease_owner="active",
    )
    before_run = repo.get_run(run_id)
    before_step = repo.get_step(step["id"])
    before_attempts = repo.list_step_attempts(step["id"])
    with pytest.raises(DurableRunStateError):
        repo.transition_run(
            run_id, expected_status="RUNNING", new_status=transition,
            lease_owner="active",
        )
    assert repo.get_run(run_id) == before_run
    assert repo.get_step(step["id"]) == before_step
    assert repo.list_step_attempts(step["id"]) == before_attempts


@pytest.mark.parametrize("transition", [
    "PAUSED", "COMPLETED", "FAILED_RETRYABLE", "FAILED_TERMINAL",
    "INTERRUPTED", "NEEDS_ATTENTION", "CANCELLED",
])
@pytest.mark.parametrize("malformed_receipt", ["active_step", "attempt_only"])
def test_all_run_exits_reject_in_flight_step_or_attempt_without_changes(
    tmp_path: Path, transition: str, malformed_receipt: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="owner",
    )
    step = repo.create_step(
        run_id=run_id, step_key="generation", input_fingerprint="v1",
        lease_owner="owner",
    )
    repo.start_step(step["id"], input_fingerprint="v1", lease_owner="owner")
    if malformed_receipt == "attempt_only":
        repo.finish_step(step["id"], status="COMPLETED", lease_owner="owner")
        # Legacy corruption: current Step already says COMPLETED,
        # but the durable Attempt remains RUNNING.
        with repository_write(db) as conn:
            conn.execute(
                "UPDATE run_step_attempts SET status = 'RUNNING' "
                "WHERE run_step_id = ?", (step["id"],),
            )
    pre_run = repo.get_run(run_id)
    pre_step = repo.get_step(step["id"])
    pre_attempts = repo.list_step_attempts(step["id"])
    pre_lease = repo.inspect_lease("work_test")
    with pytest.raises(DurableRunStateError, match="reconcile"):
        repo.transition_run(
            run_id, expected_status="RUNNING", new_status=transition,
            lease_owner="owner",
        )
    assert repo.get_run(run_id) == pre_run
    assert repo.get_step(step["id"]) == pre_step
    assert repo.list_step_attempts(step["id"]) == pre_attempts
    assert repo.inspect_lease("work_test") == pre_lease


@pytest.mark.parametrize("step_status,run_status", [
    ("COMPLETED", "COMPLETED"),
    ("FAILED_RETRYABLE", "FAILED_RETRYABLE"),
    ("FAILED_TERMINAL", "FAILED_TERMINAL"),
    ("INTERRUPTED", "INTERRUPTED"),
    ("NEEDS_ATTENTION", "NEEDS_ATTENTION"),
    ("CANCELLED", "CANCELLED"),
    ("COMPLETED", "PAUSED"),
])
def test_reconciled_step_attempt_allows_legitimate_run_exit(
    tmp_path: Path, step_status: str, run_status: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="active",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="active",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="active",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1", lease_owner="active")
    repo.finish_step(step_id, status=step_status, lease_owner="active")
    changed = repo.transition_run(
        run_id, expected_status="RUNNING",
        new_status=run_status, lease_owner="active",
    )
    assert changed["status"] == run_status
    assert repo.get_step(step_id)["status"] == step_status
    assert repo.list_step_attempts(step_id)[0]["status"] == step_status
    repo.release_lease(work_id="work_test", lease_owner="active")
    assert repo.inspect_lease("work_test") is None


def test_unleased_generic_run_exit_rejects_in_flight_receipts(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="unleased", input_fingerprint="v1",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1")
    before = repo.get_run(run_id)
    with pytest.raises(DurableRunStateError, match="reconcile"):
        repo.transition_run(
            run_id, expected_status="RUNNING", new_status="COMPLETED",
        )
    assert repo.get_run(run_id) == before
    assert repo.get_step(step_id)["status"] == "RUNNING"
    repo.finish_step(step_id, status="COMPLETED")
    assert repo.transition_run(
        run_id, expected_status="RUNNING", new_status="COMPLETED",
    )["status"] == "COMPLETED"


def test_historical_attempt_only_conflict_keeps_previous_completed_attempts(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="owner",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="render",
        input_fingerprint="v1", lease_owner="owner",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1", lease_owner="owner")
    repo.finish_step(
        step_id, status="FAILED_RETRYABLE", lease_owner="owner",
    )
    repo.start_step(step_id, input_fingerprint="v2", lease_owner="owner")
    repo.finish_step(step_id, status="COMPLETED", lease_owner="owner")
    with repository_write(db) as conn:
        conn.execute(
            "UPDATE run_step_attempts SET status = 'RUNNING' "
            "WHERE run_step_id = ? AND attempt_no = 1", (step_id,),
        )
    old_attempts = repo.list_step_attempts(step_id)
    assert [a["status"] for a in old_attempts] == ["RUNNING", "COMPLETED"]
    with pytest.raises(DurableRunStateError):
        repo.transition_run(
            run_id, expected_status="RUNNING", new_status="COMPLETED",
            lease_owner="owner",
        )
    assert repo.get_run(run_id)["status"] == "RUNNING"
    assert repo.get_step(step_id)["status"] == "COMPLETED"
    assert repo.list_step_attempts(step_id) == old_attempts


# Issue #386: independent Phase C audit-derived test-first RED matrix.
@pytest.mark.parametrize("unresolved_step", [
    "PENDING", "STALE", "FAILED_RETRYABLE", "FAILED_TERMINAL",
    "INTERRUPTED", "NEEDS_ATTENTION", "CANCELLED",
])
def test_phase_c_audit_completed_run_requires_reconciled_successful_steps(
    tmp_path: Path, unresolved_step: str,
) -> None:
    """A SUCCESS parent must not advertise unfinished/failed child work."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="original",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="original",
    )
    step = repo.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="original",
    )
    if unresolved_step != "PENDING":
        repo.start_step(
            step["id"], input_fingerprint="v1", lease_owner="original",
        )
        repo.finish_step(
            step["id"],
            status="COMPLETED" if unresolved_step == "STALE" else unresolved_step,
            lease_owner="original",
        )
        if unresolved_step == "STALE":
            repo.mark_step_stale(
                step["id"], new_fingerprint="v2", lease_owner="original",
            )
    assert repo.get_step(step["id"])["status"] == unresolved_step
    old_run = repo.get_run(run_id)
    old_step = repo.get_step(step["id"])
    old_attempts = repo.list_step_attempts(step["id"])
    old_lease = repo.inspect_lease("work_test")
    with pytest.raises(DurableRunStateError):
        repo.transition_run(
            run_id, expected_status="RUNNING", new_status="COMPLETED",
            lease_owner="original",
        )
    assert repo.get_run(run_id) == old_run
    assert repo.get_step(step["id"]) == old_step
    assert repo.list_step_attempts(step["id"]) == old_attempts
    assert repo.inspect_lease("work_test") == old_lease


def test_completed_run_accepts_fully_reconciled_multiple_steps_and_retry_history(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="owner",
    )
    first = repo.create_step(
        run_id=run_id, step_key="plan_story",
        input_fingerprint="story_v1", lease_owner="owner",
    )["id"]
    second = repo.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="panels_v1", lease_owner="owner",
    )["id"]
    repo.start_step(first, input_fingerprint="story_v1", lease_owner="owner")
    repo.finish_step(first, status="COMPLETED", lease_owner="owner")
    repo.start_step(second, input_fingerprint="panels_v1", lease_owner="owner")
    repo.finish_step(second, status="FAILED_RETRYABLE", lease_owner="owner")
    repo.start_step(second, input_fingerprint="panels_v2", lease_owner="owner")
    repo.finish_step(second, status="COMPLETED", lease_owner="owner")
    assert [attempt["status"] for attempt in repo.list_step_attempts(second)] == [
        "FAILED_RETRYABLE", "COMPLETED",
    ]
    assert repo.transition_run(
        run_id, expected_status="RUNNING", new_status="COMPLETED",
        lease_owner="owner",
    )["status"] == "COMPLETED"
    repo.release_lease(work_id="work_test", lease_owner="owner")
    assert repo.inspect_lease("work_test") is None


@pytest.mark.parametrize("run_exit", [
    "FAILED_RETRYABLE", "FAILED_TERMINAL", "PAUSED",
    "INTERRUPTED", "NEEDS_ATTENTION", "CANCELLED",
])
def test_non_successful_run_exit_allows_pending_downstream_receipt(
    tmp_path: Path, run_exit: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="owner",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="pending_downstream",
        input_fingerprint="v1", lease_owner="owner",
    )["id"]
    assert repo.transition_run(
        run_id, expected_status="RUNNING", new_status=run_exit,
        lease_owner="owner",
    )["status"] == run_exit
    assert repo.get_step(step_id)["status"] == "PENDING"
    assert repo.list_step_attempts(step_id) == []
    repo.release_lease(work_id="work_test", lease_owner="owner")


@pytest.mark.parametrize("latest_status", ["FAILED_TERMINAL", "INTERRUPTED"])
def test_completed_run_refuses_malformed_latest_attempt_without_mutation(
    tmp_path: Path, latest_status: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="owner",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="owner",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1", lease_owner="owner")
    repo.finish_step(step_id, status="COMPLETED", lease_owner="owner")
    # Simulate a legacy corrupted receipt, not a normal product API mutation.
    with repository_write(db) as conn:
        conn.execute(
            "UPDATE run_step_attempts SET status = ? WHERE run_step_id = ?",
            (latest_status, step_id),
        )
    old_run = repo.get_run(run_id)
    old_step = repo.get_step(step_id)
    old_attempts = repo.list_step_attempts(step_id)
    old_lease = repo.inspect_lease("work_test")
    with pytest.raises(DurableRunStateError):
        repo.transition_run(
            run_id, expected_status="RUNNING", new_status="COMPLETED",
            lease_owner="owner",
        )
    assert repo.get_run(run_id) == old_run
    assert repo.get_step(step_id) == old_step
    assert repo.list_step_attempts(step_id) == old_attempts
    assert repo.inspect_lease("work_test") == old_lease


def test_completed_run_with_no_registered_steps_still_allowed(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
    )
    assert repo.transition_run(
        run_id, expected_status="RUNNING", new_status="COMPLETED",
    )["status"] == "COMPLETED"


# #387: independent audit RED cases; intended to turn GREEN after fix.
@pytest.mark.parametrize("terminal_status", [
    "COMPLETED", "FAILED_TERMINAL", "CANCELLED",
])
def test_phase_c_audit_terminal_run_cannot_gain_step_after_lease_release(
    tmp_path: Path, terminal_status: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="original",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="original",
    )
    original_step = repo.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="original",
    )
    repo.start_step(
        original_step["id"], input_fingerprint="v1",
        lease_owner="original",
    )
    repo.finish_step(
        original_step["id"], status=terminal_status,
        lease_owner="original",
    )
    repo.transition_run(
        run_id, expected_status="RUNNING", new_status=terminal_status,
        lease_owner="original",
    )
    repo.release_lease(work_id="work_test", lease_owner="original")
    frozen_run = repo.get_run(run_id)
    frozen_steps = repo.list_steps(run_id)
    with pytest.raises(DurableRunStateError):
        repo.create_step(
            run_id=run_id, step_key="new_unrecorded_work",
            input_fingerprint="v2",
        )
    assert repo.get_run(run_id) == frozen_run
    assert repo.list_steps(run_id) == frozen_steps
    assert repo.inspect_lease("work_test") is None


def test_phase_c_audit_completed_run_step_cannot_be_staled_after_release(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="original",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="original",
    )
    step = repo.create_step(
        run_id=run_id, step_key="plan_story",
        input_fingerprint="v1", lease_owner="original",
    )
    repo.start_step(step["id"], input_fingerprint="v1", lease_owner="original")
    repo.finish_step(
        step["id"], status="COMPLETED",
        output={"value": "verified"}, lease_owner="original",
    )
    repo.transition_run(
        run_id, expected_status="RUNNING", new_status="COMPLETED",
        lease_owner="original",
    )
    repo.release_lease(work_id="work_test", lease_owner="original")
    frozen = repo.get_step(step["id"])
    before_attempts = repo.list_step_attempts(step["id"])
    with pytest.raises(DurableRunStateError):
        repo.mark_step_stale(step["id"], new_fingerprint="v2")
    assert repo.get_run(run_id)["status"] == "COMPLETED"
    assert repo.get_step(step["id"]) == frozen
    assert repo.list_step_attempts(step["id"]) == before_attempts


def test_phase_c_audit_cancelled_pending_run_step_cannot_change_fingerprint(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    step = repo.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1",
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="CANCELLED",
    )
    before = repo.get_step(step["id"])
    with pytest.raises(DurableRunStateError):
        repo.set_pending_fingerprint(step["id"], input_fingerprint="v2")
    assert repo.get_run(run_id)["status"] == "CANCELLED"
    assert repo.get_step(step["id"]) == before


@pytest.mark.parametrize("terminal_status", [
    "COMPLETED", "FAILED_TERMINAL", "CANCELLED",
])
def test_terminal_run_rejects_all_step_edits_before_work_lease_release(
    tmp_path: Path, terminal_status: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="live_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="live_owner",
    )
    finished = repo.create_step(
        run_id=run_id, step_key="generation",
        input_fingerprint="v1", lease_owner="live_owner",
    )["id"]
    pending = None
    if terminal_status != "COMPLETED":
        pending = repo.create_step(
            run_id=run_id, step_key="downstream",
            input_fingerprint="v1", lease_owner="live_owner",
        )["id"]
    repo.start_step(finished, input_fingerprint="v1", lease_owner="live_owner")
    repo.finish_step(
        finished, status=terminal_status, lease_owner="live_owner",
    )
    repo.transition_run(
        run_id, expected_status="RUNNING", new_status=terminal_status,
        lease_owner="live_owner",
    )
    before_run = repo.get_run(run_id)
    before_steps = repo.list_steps(run_id)
    before_attempts = repo.list_step_attempts(finished)
    before_lease = repo.inspect_lease("work_test")
    with pytest.raises(WorkLeaseConflictError, match="stale or expired"):
        repo.create_step(
            run_id=run_id, step_key="appended", input_fingerprint="v2",
            lease_owner="live_owner",
        )
    if terminal_status == "COMPLETED":
        with pytest.raises(WorkLeaseConflictError, match="stale or expired"):
            repo.mark_step_stale(
                finished, new_fingerprint="v2", lease_owner="live_owner",
            )
    else:
        assert pending is not None
        with pytest.raises(WorkLeaseConflictError, match="stale or expired"):
            repo.set_pending_fingerprint(
                pending, input_fingerprint="v2", lease_owner="live_owner",
            )
    assert repo.get_run(run_id) == before_run
    assert repo.list_steps(run_id) == before_steps
    assert repo.list_step_attempts(finished) == before_attempts
    assert repo.inspect_lease("work_test") == before_lease
    repo.release_lease(work_id="work_test", lease_owner="live_owner")


@pytest.mark.parametrize("nonterminal", [
    "PENDING", "RUNNING", "PAUSED", "FAILED_RETRYABLE",
    "INTERRUPTED", "NEEDS_ATTENTION",
])
def test_nonterminal_run_preserves_generic_pending_fingerprint_mutations(
    tmp_path: Path, nonterminal: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    if nonterminal != "PENDING":
        repo.transition_run(
            run_id, expected_status="PENDING", new_status="RUNNING",
        )
        if nonterminal != "RUNNING":
            repo.transition_run(
                run_id, expected_status="RUNNING", new_status=nonterminal,
            )
    step = repo.create_step(
        run_id=run_id, step_key="late_pending", input_fingerprint="v1",
    )["id"]
    repo.set_pending_fingerprint(step, input_fingerprint="v2")
    assert repo.get_step(step)["status"] == "PENDING"
    assert repo.get_step(step)["input_fingerprint"] == "v2"


def test_completed_step_can_be_staled_during_running_parent_but_not_after(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="owner",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="plan_story",
        input_fingerprint="v1", lease_owner="owner",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1", lease_owner="owner")
    repo.finish_step(step_id, status="COMPLETED", lease_owner="owner")
    repo.mark_step_stale(step_id, new_fingerprint="v2", lease_owner="owner")
    assert repo.get_step(step_id)["status"] == "STALE"
    repo.start_step(step_id, input_fingerprint="v2", lease_owner="owner")
    repo.finish_step(step_id, status="COMPLETED", lease_owner="owner")
    assert repo.transition_run(
        run_id, expected_status="RUNNING", new_status="COMPLETED",
        lease_owner="owner",
    )["status"] == "COMPLETED"
    with pytest.raises(WorkLeaseConflictError, match="stale or expired"):
        repo.mark_step_stale(
            step_id, new_fingerprint="v3", lease_owner="owner",
        )
    assert [a["status"] for a in repo.list_step_attempts(step_id)] == [
        "COMPLETED", "COMPLETED",
    ]


@pytest.mark.parametrize("late_op", ["finish", "heartbeat"])
def test_legacy_terminal_parent_cannot_rewrite_running_step_attempt(
    tmp_path: Path, late_op: str,
) -> None:
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="owner",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="generation",
        input_fingerprint="v1", lease_owner="owner",
    )["id"]
    repo.start_step(step_id, input_fingerprint="v1", lease_owner="owner")
    # Historical inconsistent terminal snapshot, not a production transition.
    with repository_write(db) as conn:
        conn.execute(
            "UPDATE runs SET status = 'CANCELLED', lease_owner = NULL "
            "WHERE id = ?", (run_id,),
        )
    before = repo.get_step(step_id)
    attempts = repo.list_step_attempts(step_id)
    lease = repo.inspect_lease("work_test")
    with pytest.raises(WorkLeaseConflictError, match="stale or expired"):
        if late_op == "heartbeat":
            repo.heartbeat_step(step_id, lease_owner="owner")
        else:
            repo.finish_step(
                step_id, status="CANCELLED", lease_owner="owner",
            )
    assert repo.get_step(step_id) == before
    assert repo.list_step_attempts(step_id) == attempts
    assert repo.inspect_lease("work_test") == lease


def test_phase_c_audit_heartbeat_must_not_revive_expired_lease_during_lock_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshotting time before BEGIN IMMEDIATE must not extend expired ownership."""
    from contextlib import contextmanager

    import manga_autopilot.repositories.durable_runs as durable_module

    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    old = repo.acquire_lease(
        work_id="work_test", lease_owner="prior_owner",
        lease_kind="MUTATION", ttl_seconds=3,
    )

    # At method entry the lease is live, but the BEGIN IMMEDIATE writer
    # cannot enter until *after* the prior lease has expired. This injection
    # models a contended SQLite writer deterministically without real sleeps.
    clock.advance(2)
    original_write = durable_module.repository_write

    @contextmanager
    def delayed_write(database_path):
        clock.advance(2)
        with original_write(database_path) as connection:
            yield connection

    monkeypatch.setattr(durable_module, "repository_write", delayed_write)
    with pytest.raises(WorkLeaseConflictError, match="expired"):
        repo.heartbeat_lease(
            work_id="work_test", lease_owner="prior_owner", ttl_seconds=3,
        )
    after = repo.inspect_lease("work_test")
    assert after is not None
    assert after["expires_at"] == old["expires_at"]
    assert after["heartbeat_at"] == old["heartbeat_at"]
    assert after["expired"] is True


def test_phase_c_audit_expired_lease_recovery_uses_transaction_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching-token recovery must observe expiration after writer wait."""
    from contextlib import contextmanager

    import manga_autopilot.repositories.durable_runs as durable_module

    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    original = repo.acquire_lease(
        work_id="work_test", lease_owner="prior_owner",
        lease_kind="MUTATION", ttl_seconds=3,
    )
    clock.advance(2)
    original_write = durable_module.repository_write

    @contextmanager
    def delayed_write(database_path):
        clock.advance(2)
        with original_write(database_path) as connection:
            yield connection

    monkeypatch.setattr(durable_module, "repository_write", delayed_write)
    recovered = repo.acquire_lease(
        work_id="work_test", lease_owner="fresh_owner", lease_kind="MUTATION",
        ttl_seconds=10, reclaim_expired_owner="prior_owner",
    )
    assert recovered["lease_owner"] == "fresh_owner"
    assert recovered["heartbeat_at"] > original["expires_at"]
    assert repo.inspect_lease("work_test")["expired"] is False


def test_phase_c_audit_lease_heartbeat_live_without_writer_delay(
    tmp_path: Path,
) -> None:
    """Control: normal renewal still extends a genuinely live lease."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    first = repo.acquire_lease(
        work_id="work_test", lease_owner="prior_owner",
        lease_kind="MUTATION", ttl_seconds=3,
    )
    clock.advance(2)
    renewed = repo.heartbeat_lease(
        work_id="work_test", lease_owner="prior_owner", ttl_seconds=3,
    )
    assert renewed["expires_at"] > first["expires_at"]
    assert repo.inspect_lease("work_test")["expired"] is False


def test_phase_c_audit_expired_lease_after_writer_wait_fences_run_step_and_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected late renewal must retain a recoverable owner fence."""
    from contextlib import contextmanager

    import manga_autopilot.repositories.durable_runs as durable_module
    from manga_autopilot.repositories.page_domain import PageRepository
    from manga_autopilot.storage.repository import (
        WorkMutationLeaseConflictError,
        owned_work_mutation,
    )

    db = tmp_path / "work.sqlite3"
    _work(db)
    pages = PageRepository(db)
    page = pages.create_page(
        page_id="p0", page_number=1, order_key="1",
        page_purpose="before lease",
    )
    clock = Clock()
    old_owner = DurableRunRepository(db, clock=clock)
    run_id = _run(old_owner)
    old_owner.acquire_lease(
        work_id="work_test", lease_owner="prior_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    old_owner.transition_run(
        run_id, expected_status="PENDING",
        new_status="RUNNING", lease_owner="prior_owner",
    )
    step = old_owner.create_step(
        run_id=run_id, step_key="render", input_fingerprint="v1",
        lease_owner="prior_owner",
    )
    old_owner.start_step(
        step["id"], input_fingerprint="v1", lease_owner="prior_owner",
    )
    before_run = old_owner.get_run(run_id)
    before_step = old_owner.get_step(step["id"])
    before_attempt = old_owner.list_step_attempts(step["id"])
    before_lease = old_owner.inspect_lease("work_test")

    clock.advance(2)
    original_write = durable_module.repository_write

    @contextmanager
    def delayed_write(path):
        # Simulate SQLite BEGIN IMMEDIATE acquiring its lock only at T0+4.
        clock.advance(2)
        with original_write(path) as connection:
            yield connection

    with monkeypatch.context() as patch:
        patch.setattr(durable_module, "repository_write", delayed_write)
        with pytest.raises(WorkLeaseConflictError, match="expired"):
            old_owner.heartbeat_lease(
                work_id="work_test", lease_owner="prior_owner", ttl_seconds=3,
            )
    assert old_owner.get_run(run_id) == before_run
    assert old_owner.get_step(step["id"]) == before_step
    assert old_owner.list_step_attempts(step["id"]) == before_attempt
    assert old_owner.inspect_lease("work_test")["expires_at"] == before_lease["expires_at"]
    assert old_owner.inspect_lease("work_test")["expired"] is True
    with pytest.raises(WorkLeaseConflictError):
        old_owner.heartbeat_run(run_id, lease_owner="prior_owner")
    with pytest.raises(WorkLeaseConflictError):
        old_owner.heartbeat_step(step["id"], lease_owner="prior_owner")
    with owned_work_mutation("work_test", "prior_owner"):
        with pytest.raises(WorkMutationLeaseConflictError):
            pages.update_page(
                page["id"], expected_revision=page["revision"],
                page_purpose="stale writer",
            )
    assert pages.get_page(page["id"]) == page

    replacement = DurableRunRepository(db, clock=clock)
    rotated = replacement.acquire_lease(
        work_id="work_test", lease_owner="rotated_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
        run_id=run_id, reclaim_expired_owner="prior_owner",
    )
    assert rotated["lease_owner"] == "rotated_owner"
    replacement.recover_interrupted_run(run_id, lease_owner="rotated_owner")
    assert replacement.get_run(run_id)["status"] == "INTERRUPTED"
    assert replacement.get_step(step["id"])["status"] == "INTERRUPTED"
    assert replacement.list_step_attempts(step["id"])[0]["status"] == "INTERRUPTED"
    with pytest.raises(WorkLeaseConflictError):
        old_owner.heartbeat_lease(
            work_id="work_test", lease_owner="prior_owner", ttl_seconds=3,
        )
    replacement.release_lease(work_id="work_test", lease_owner="rotated_owner")


# Independent Phase C post-#393 audit — privileged interrupted recovery must
# be backed by an expired, previously Run-bound Work mutation lease.
@pytest.mark.parametrize("new_lease_origin", ["fresh", "expired_unbound"])
def test_phase_c_audit_recovery_requires_previously_owned_run(
    tmp_path: Path, new_lease_origin: str,
) -> None:
    """Fresh lease attachment must not fabricate an old crashed owner.

    Generic Work Runs may legitimately start unleased, but recovering one
    requires evidence of a *prior leased* worker that actually crashed.
    """
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    original = DurableRunRepository(db, clock=clock)
    recovery = DurableRunRepository(db, clock=clock)
    run_id = _run(original)
    original.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
    )
    step = original.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1",
    )
    original.start_step(step["id"], input_fingerprint="v1")
    before_run = original.get_run(run_id)
    before_step = original.get_step(step["id"])
    before_attempts = original.list_step_attempts(step["id"])
    assert before_run["lease_owner"] is None
    assert before_step["status"] == "RUNNING"

    reclaim = None
    if new_lease_origin == "expired_unbound":
        original.acquire_lease(
            work_id="work_test", lease_owner="maintenance",
            lease_kind="MUTATION", ttl_seconds=3,
        )
        clock.advance(4)
        reclaim = "maintenance"
    before_lease = recovery.inspect_lease("work_test")
    # Fail at the earliest transaction boundary. Otherwise an attached
    # lease on an ownerless RUNNING Run cannot be ordinarily released (#382).
    with pytest.raises(WorkLeaseConflictError, match="prior|previous|reclaim"):
        recovery.acquire_lease(
            work_id="work_test", lease_owner="new_owner",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
            run_id=run_id, reclaim_expired_owner=reclaim,
        )

    assert recovery.get_run(run_id) == before_run
    assert recovery.get_step(step["id"]) == before_step
    assert recovery.list_step_attempts(step["id"]) == before_attempts
    assert recovery.inspect_lease("work_test") == before_lease


def test_phase_c_audit_genuine_expired_owner_recovery_preserves_receipts(
    tmp_path: Path,
) -> None:
    """Positive control: exact previous bound owner is still reclaimable."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    original = DurableRunRepository(db, clock=clock)
    recovery = DurableRunRepository(db, clock=clock)
    run_id = _run(original)
    original.acquire_lease(
        work_id="work_test", lease_owner="crashed_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    original.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="crashed_owner",
    )
    step = original.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="crashed_owner",
    )
    original.start_step(
        step["id"], input_fingerprint="v1", lease_owner="crashed_owner",
    )
    clock.advance(4)
    recovery.acquire_lease(
        work_id="work_test", lease_owner="rotated_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
        run_id=run_id, reclaim_expired_owner="crashed_owner",
    )
    recovery.recover_interrupted_run(run_id, lease_owner="rotated_owner")
    assert recovery.get_run(run_id)["status"] == "INTERRUPTED"
    assert recovery.get_step(step["id"])["status"] == "INTERRUPTED"
    assert recovery.list_step_attempts(step["id"])[0]["status"] == "INTERRUPTED"


@pytest.mark.parametrize("orphan_origin", ["missing", "expired_unbound"])
def test_issue395_malformed_prior_owner_cannot_fake_crash_recovery(
    tmp_path: Path, orphan_origin: str,
) -> None:
    """A stale Run owner field alone is not proof of an expired bound lease."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    original = DurableRunRepository(db, clock=clock)
    fresh = DurableRunRepository(db, clock=clock)
    run_id = _run(original)
    original.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
    )
    step = original.create_step(
        run_id=run_id, step_key="generation", input_fingerprint="v1",
    )
    original.start_step(step["id"], input_fingerprint="v1")
    # Fixture-only malformed legacy snapshot: production transition_run()
    # cannot legitimately leave this field set without a matching lease.
    with repository_write(db) as conn:
        conn.execute(
            "UPDATE runs SET lease_owner = 'ghost_old_owner' WHERE id = ?",
            (run_id,),
        )

    reclaim = None
    if orphan_origin == "expired_unbound":
        original.acquire_lease(
            work_id="work_test", lease_owner="maintenance",
            lease_kind="MUTATION", ttl_seconds=3,
        )
        clock.advance(4)
        reclaim = "maintenance"
    old_run = original.get_run(run_id)
    old_step = original.get_step(step["id"])
    old_attempts = original.list_step_attempts(step["id"])
    old_lease = original.inspect_lease("work_test")

    with pytest.raises(WorkLeaseConflictError, match="prior|previous|reclaim"):
        fresh.acquire_lease(
            work_id="work_test", lease_owner="fresh_owner",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
            run_id=run_id, reclaim_expired_owner=reclaim,
        )
    assert fresh.get_run(run_id) == old_run
    assert fresh.get_step(step["id"]) == old_step
    assert fresh.list_step_attempts(step["id"]) == old_attempts
    assert fresh.inspect_lease("work_test") == old_lease


def test_issue395_valid_recovery_rejects_late_original_owner(
    tmp_path: Path,
) -> None:
    """Successful rotated same-Run recovery continues to fence the old token."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="lost",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="lost",
    )
    clock.advance(4)
    repo.acquire_lease(
        work_id="work_test", lease_owner="recovered",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=12,
        run_id=run_id, reclaim_expired_owner="lost",
    )
    repo.recover_interrupted_run(run_id, lease_owner="recovered")
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    with pytest.raises(WorkLeaseConflictError):
        repo.heartbeat_lease(
            work_id="work_test", lease_owner="lost", ttl_seconds=3,
        )
    with pytest.raises(WorkLeaseConflictError):
        repo.heartbeat_run(run_id, lease_owner="lost")
    repo.release_lease(work_id="work_test", lease_owner="recovered")


def test_issue395_ownerless_running_run_remains_generically_reconcilable(
    tmp_path: Path,
) -> None:
    """Rejected lease must not strand unleased generic Run/Step/Attempt."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    repo = DurableRunRepository(db)
    run_id = _run(repo)
    repo.transition_run(run_id, expected_status="PENDING", new_status="RUNNING")
    step = repo.create_step(
        run_id=run_id, step_key="render", input_fingerprint="v1",
    )
    repo.start_step(step["id"], input_fingerprint="v1")
    assert repo.inspect_lease("work_test") is None

    with pytest.raises(WorkLeaseConflictError, match="prior"):
        repo.acquire_lease(
            work_id="work_test", lease_owner="new",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=run_id,
        )
    assert repo.inspect_lease("work_test") is None

    repo.finish_step(step["id"], status="INTERRUPTED")
    repo.transition_run(
        run_id, expected_status="RUNNING", new_status="INTERRUPTED",
    )
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    assert repo.get_step(step["id"])["status"] == "INTERRUPTED"
    assert repo.list_step_attempts(step["id"])[0]["status"] == "INTERRUPTED"


def test_issue395_privileged_recovery_fences_legacy_ownerless_run_with_lease(
    tmp_path: Path,
) -> None:
    """Recovery itself must still reject malformed, already attached leases."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    repo.transition_run(run_id, expected_status="PENDING", new_status="RUNNING")
    step = repo.create_step(
        run_id=run_id, step_key="render", input_fingerprint="v1",
    )
    repo.start_step(step["id"], input_fingerprint="v1")
    # Fixture-only corrupted historical snapshot. The new acquire_lease()
    # correctly refuses to manufacture this binding through production APIs.
    with repository_write(db) as conn:
        conn.execute(
            """INSERT INTO work_leases (
                work_id, lease_owner, lease_kind, run_id, heartbeat_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "work_test", "fabricated", "AUTOPILOT_MUTATION", run_id,
                clock.now.isoformat(),
                (clock.now + timedelta(seconds=10)).isoformat(),
            ),
        )
    before_run = repo.get_run(run_id)
    before_step = repo.get_step(step["id"])
    before_attempts = repo.list_step_attempts(step["id"])
    before_lease = repo.inspect_lease("work_test")
    with pytest.raises(WorkLeaseConflictError, match="prior"):
        repo.recover_interrupted_run(run_id, lease_owner="fabricated")
    assert repo.get_run(run_id) == before_run
    assert repo.get_step(step["id"]) == before_step
    assert repo.list_step_attempts(step["id"]) == before_attempts
    assert repo.inspect_lease("work_test") == before_lease


# Independent Phase C post-#395 audit: two crashes around lease transfer
# must not permanently strand the original RUNNING Run.
@pytest.mark.parametrize("with_active_step", [False, True])
def test_phase_c_audit_second_crash_after_lease_rotation_remains_recoverable(
    tmp_path: Path, with_active_step: bool,
) -> None:
    """Crash A -> rotate to B -> crash B pre-reconciliation -> rotate to C.

    This uses only repository-supported operations, never fixture-only SQL.
    B acquires legitimately after A expires. It stops before calling
    recover_interrupted_run(), preserving the live Run and optional Attempt.
    Once B itself expires, an authorized new owner with B's exact token must
    be able to reestablish exclusive ownership and reconcile safely.
    """
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    crashed = DurableRunRepository(db, clock=clock)
    first_restart = DurableRunRepository(db, clock=clock)
    second_restart = DurableRunRepository(db, clock=clock)
    run_id = _run(crashed)
    crashed.acquire_lease(
        work_id="work_test", lease_owner="crashed_A",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    crashed.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="crashed_A",
    )
    step_id = None
    if with_active_step:
        step_id = crashed.create_step(
            run_id=run_id, step_key="generate_panels",
            input_fingerprint="v1", lease_owner="crashed_A",
        )["id"]
        crashed.start_step(
            step_id, input_fingerprint="v1", lease_owner="crashed_A",
        )
    clock.advance(4)
    first_restart.acquire_lease(
        work_id="work_test", lease_owner="crashed_B",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3,
        run_id=run_id, reclaim_expired_owner="crashed_A",
    )
    # The first recovery process dies after its durable lease commit,
    # but *before* recovery can rewrite any old Run or child receipts.
    previous_run = second_restart.get_run(run_id)
    previous_steps = second_restart.list_steps(run_id)
    previous_attempts = (
        second_restart.list_step_attempts(step_id) if step_id else []
    )
    assert previous_run["status"] == "RUNNING"
    assert previous_run["lease_owner"] == "crashed_A"
    assert second_restart.inspect_lease("work_test")["lease_owner"] == "crashed_B"

    clock.advance(4)
    before_reclaim = second_restart.inspect_lease("work_test")
    assert before_reclaim is not None and before_reclaim["expired"] is True
    with pytest.raises(WorkLeaseConflictError, match="explicit"):
        second_restart.acquire_lease(
            work_id="work_test", lease_owner="recovered_C",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
            run_id=run_id, reclaim_expired_owner="crashed_A",
        )
    assert second_restart.inspect_lease("work_test") == before_reclaim
    assert second_restart.get_run(run_id) == previous_run
    assert second_restart.list_steps(run_id) == previous_steps
    if step_id:
        assert second_restart.list_step_attempts(step_id) == previous_attempts

    fresh_lease = second_restart.acquire_lease(
        work_id="work_test", lease_owner="recovered_C",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
        run_id=run_id, reclaim_expired_owner="crashed_B",
    )
    assert fresh_lease["lease_owner"] == "recovered_C"
    second_restart.recover_interrupted_run(
        run_id, lease_owner="recovered_C",
    )
    assert second_restart.get_run(run_id)["status"] == "INTERRUPTED"
    if step_id:
        assert second_restart.get_step(step_id)["status"] == "INTERRUPTED"
        assert second_restart.list_step_attempts(step_id)[0]["status"] == "INTERRUPTED"
    with pytest.raises(WorkLeaseConflictError):
        first_restart.heartbeat_lease(
            work_id="work_test", lease_owner="crashed_B", ttl_seconds=3,
        )
    second_restart.release_lease(
        work_id="work_test", lease_owner="recovered_C",
    )


def test_phase_c_audit_single_crash_expired_bound_owner_control(
    tmp_path: Path,
) -> None:
    """Positive control: a single exact-owner expired recovery still works."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="crashed_A",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="crashed_A",
    )
    clock.advance(4)
    repo.acquire_lease(
        work_id="work_test", lease_owner="recovered_B",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
        run_id=run_id, reclaim_expired_owner="crashed_A",
    )
    repo.recover_interrupted_run(run_id, lease_owner="recovered_B")
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    repo.release_lease(work_id="work_test", lease_owner="recovered_B")


def test_issue397_v7_work_lease_upgrade_preserves_tombstone_without_forging_lineage(
    tmp_path: Path,
) -> None:
    """Schema upgrade cannot assert provenance for pre-v8 historical rows."""
    db = tmp_path / "work.sqlite3"
    _work(db, migrations=WORK_MIGRATIONS[:7])
    old = DurableRunRepository(db)
    run_id = _run(old)
    with repository_write(db) as conn:
        conn.execute(
            """INSERT INTO work_leases
               (work_id, lease_owner, lease_kind, run_id, heartbeat_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "work_test", "old_worker", "AUTOPILOT_MUTATION", run_id,
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:03+00:00",
            ),
        )
    result = bootstrap_work_database(db, work_id="work_test")
    assert result.migration.applied_versions == (8,)
    assert result.migration.backup_path is not None
    with repository_read(db) as conn:
        row = conn.execute(
            "SELECT lease_owner, run_id, recovery_run_owner "
            "FROM work_leases WHERE work_id = 'work_test'"
        ).fetchone()
        assert row is not None
        assert row["lease_owner"] == "old_worker"
        assert row["run_id"] == run_id
        assert row["recovery_run_owner"] is None
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_issue397_double_rotation_persists_original_run_provenance(
    tmp_path: Path,
) -> None:
    """An attested A->B->C chain must carry A until explicit reconciliation."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="A",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING",
        new_status="RUNNING", lease_owner="A",
    )
    clock.advance(4)
    b = repo.acquire_lease(
        work_id="work_test", lease_owner="B",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3,
        run_id=run_id, reclaim_expired_owner="A",
    )
    assert b["recovery_run_owner"] == "A"
    clock.advance(4)
    c = repo.acquire_lease(
        work_id="work_test", lease_owner="C",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3,
        run_id=run_id, reclaim_expired_owner="B",
    )
    assert c["recovery_run_owner"] == "A"
    assert repo.get_run(run_id)["lease_owner"] == "A"
    clock.advance(4)
    d = repo.acquire_lease(
        work_id="work_test", lease_owner="D",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=9,
        run_id=run_id, reclaim_expired_owner="C",
    )
    assert d["recovery_run_owner"] == "A"
    repo.recover_interrupted_run(run_id, lease_owner="D")
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    repo.release_lease(work_id="work_test", lease_owner="D")


def test_issue397_orphan_rotated_row_without_matching_lineage_is_rejected(
    tmp_path: Path,
) -> None:
    """A mismatched lineage field never grants synthetic crash authority."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="A",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING",
        new_status="RUNNING", lease_owner="A",
    )
    clock.advance(4)
    repo.acquire_lease(
        work_id="work_test", lease_owner="B",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3,
        run_id=run_id, reclaim_expired_owner="A",
    )
    with repository_write(db) as conn:
        # Fixture-only history corruption: no durable attestation to A.
        conn.execute(
            "UPDATE work_leases SET recovery_run_owner = 'wrong_origin' "
            "WHERE work_id = 'work_test'",
        )
    clock.advance(4)
    saved = repo.inspect_lease("work_test")
    with pytest.raises(WorkLeaseConflictError, match="prior"):
        repo.acquire_lease(
            work_id="work_test", lease_owner="C",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
            run_id=run_id, reclaim_expired_owner="B",
        )
    assert repo.inspect_lease("work_test") == saved
    assert repo.get_run(run_id)["status"] == "RUNNING"


def test_issue397_post_recovery_next_run_cannot_inherit_old_chain(
    tmp_path: Path,
) -> None:
    """Provenance is scoped to one unreconciled Run only."""
    db = tmp_path / "work.sqlite3"
    _work(db)
    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = _run(repo)
    another_id = _run(repo)
    repo.acquire_lease(
        work_id="work_test", lease_owner="A",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=3, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="A",
    )
    clock.advance(4)
    b = repo.acquire_lease(
        work_id="work_test", lease_owner="B",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
        run_id=run_id, reclaim_expired_owner="A",
    )
    assert b["recovery_run_owner"] == "A"
    repo.recover_interrupted_run(run_id, lease_owner="B")
    repo.release_lease(work_id="work_test", lease_owner="B")
    next_lease = repo.acquire_lease(
        work_id="work_test", lease_owner="new",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=another_id,
    )
    assert next_lease["recovery_run_owner"] is None
    repo.release_lease(work_id="work_test", lease_owner="new")
