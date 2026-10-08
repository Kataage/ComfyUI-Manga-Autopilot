"""GPU-free Work DB -> legacy hook bridge tests for Issue #246."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from manga_autopilot.repositories.durable_runs import (
    DurableRunRepository,
    DurableRunStateError,
    WorkLeaseConflictError,
)
from manga_autopilot.services.autopilot import OrchestratorHooks
from manga_autopilot.services.durable_autopilot import (
    DurableAutopilotOrchestrator,
    NeedsAttentionStepError,
    RetryableStepError,
)
from manga_autopilot.storage import (
    bootstrap_work_database,
    create_work_commit,
    create_work_entity_revision,
    repository_read,
    repository_write,
)


def work(tmp_path: Path) -> Path:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_246")
    with repository_write(database) as conn:
        commit = create_work_commit(
            conn, commit_id="init_work", actor_type="system",
            operation_type="create_work",
        )
        conn.execute(
            """INSERT INTO work_metadata (
                work_id, title, work_kind, language, reading_direction,
                status, current_commit_seq, current_revision,
                created_at, updated_at
            ) VALUES (?, ?, 'standalone', 'ja', 'RTL_TOP_TO_BOTTOM',
                      'DRAFT', ?, 1, ?, ?)""",
            ("work_246", "Durable", commit.commit_seq,
             commit.created_at, commit.created_at),
        )
        create_work_entity_revision(
            conn, revision_id="work_revision", entity_type="work",
            entity_id="work_246", entity_revision=1,
            commit_seq=commit.commit_seq, change_kind="create",
            after_state={"title": "Durable"},
        )
    return database


def start(repo: DurableRunRepository) -> str:
    return repo.create_run(
        run_kind="AUTOPILOT", scope_type="WORK", scope_id="work_246",
        requested_by="test", input_fingerprint="work:v1",
    )["id"]


def runner(repo: DurableRunRepository, hooks: OrchestratorHooks) -> DurableAutopilotOrchestrator:
    return DurableAutopilotOrchestrator(
        repository=repo, work_id="work_246", hooks=hooks,
    )


@pytest.mark.asyncio
async def test_resume_new_python_objects_skips_only_fingerprint_matching_steps(
    tmp_path: Path,
) -> None:
    db = work(tmp_path)
    first_repo = DurableRunRepository(db)
    run_id = start(first_repo)
    calls: Counter[str] = Counter()

    async def validate(run):
        calls["validate"] += 1
        return {"normalized": run.input["title"]}

    async def story(run):
        calls["story"] += 1
        assert run.artefacts["validate_input"] == {"normalized": "hello"}
        return {"plan": "p"}

    async def panels(run):
        calls["panels"] += 1
        if calls["panels"] == 1:
            raise RetryableStepError("temporary executor outage")
        assert run.artefacts["plan_story"] == {"plan": "p"}
        return {"candidate": "image"}

    async def render(run):
        calls["render"] += 1
        assert run.artefacts["generate_panels"] == {"candidate": "image"}
        return {"page": "png"}

    hooks = OrchestratorHooks(
        validate_input=validate, plan_story=story,
        generate_panels=panels, render_pages=render,
    )
    initial = await runner(first_repo, hooks).execute(
        run_id, input_payload={"title": "hello"},
        step_inputs={}, lease_owner="first_worker",
    )
    assert initial.machine.state.value == "FAILED_PANEL_GENERATION"
    assert first_repo.get_run(run_id)["status"] == "FAILED_RETRYABLE"
    before = first_repo.list_steps(run_id)
    assert len(before) == 9
    assert before[0]["status"] == "COMPLETED"
    assert before[-1]["status"] == "FAILED_RETRYABLE"

    # Re-open a fresh repository and a fresh in-memory Autopilot state machine.
    fresh_repo = DurableRunRepository(db)
    completed = await runner(fresh_repo, hooks).execute(
        run_id, input_payload={"title": "hello"},
        step_inputs={}, lease_owner="second_worker",
    )
    assert completed.machine.state.value == "COMPLETED"
    assert fresh_repo.get_run(run_id)["status"] == "COMPLETED"
    assert calls == {
        "validate": 1, "story": 1, "panels": 2, "render": 1,
    }
    steps = {s["step_key"]: s for s in fresh_repo.list_steps(run_id)}
    assert steps["generate_panels"]["attempt_count"] == 2
    history = fresh_repo.list_step_attempts(steps["generate_panels"]["id"])
    assert [h["status"] for h in history] == ["FAILED_RETRYABLE", "COMPLETED"]
    assert [h["attempt_no"] for h in history] == [1, 2]
    assert steps["render_pages"]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_changed_stage_input_invalidates_only_stage_and_downstream(
    tmp_path: Path,
) -> None:
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    calls: Counter[str] = Counter()

    async def validate(_):
        calls["validate"] += 1
        return {"fixed": True}

    async def generate(_):
        calls["generate"] += 1
        return {"version": calls["generate"]}

    async def render(_):
        calls["render"] += 1
        if calls["render"] == 1:
            raise RetryableStepError("transient")
        return {"success": True}

    hooks = OrchestratorHooks(
        validate_input=validate, generate_panels=generate,
        render_pages=render,
    )
    first = runner(repo, hooks)
    await first.execute(
        run_id, input_payload={"title": "stable"},
        step_inputs={"generate_panels": "v1"}, lease_owner="worker1",
    )
    assert repo.get_run(run_id)["status"] == "FAILED_RETRYABLE"
    after = await runner(DurableRunRepository(db), hooks).execute(
        run_id, input_payload={"title": "stable"},
        step_inputs={"generate_panels": "v2"}, lease_owner="worker2",
    )
    assert after.machine.state.value == "COMPLETED"
    assert calls == {"validate": 1, "generate": 2, "render": 2}
    steps = {s["step_key"]: s for s in repo.list_steps(run_id)}
    assert steps["validate_input"]["attempt_count"] == 1
    assert steps["generate_panels"]["attempt_count"] == 2
    assert steps["qa_panels"]["attempt_count"] == 2
    assert steps["render_pages"]["attempt_count"] == 2
    assert [x["status"] for x in repo.list_step_attempts(
        steps["generate_panels"]["id"]
    )] == ["COMPLETED", "COMPLETED"]


@pytest.mark.asyncio
async def test_unknown_hook_failure_is_terminal_not_auto_retried(
    tmp_path: Path,
) -> None:
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)

    def fail(_):
        raise ValueError("unknown effect state")

    result = await runner(
        repo, OrchestratorHooks(generate_panels=fail),
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="worker")
    assert result.machine.state.value == "FAILED_PANEL_GENERATION"
    assert repo.get_run(run_id)["status"] == "FAILED_TERMINAL"
    with pytest.raises(DurableRunStateError, match="terminal"):
        await runner(repo, OrchestratorHooks()).execute(
            run_id, input_payload={}, step_inputs={},
            lease_owner="new_worker",
        )


@pytest.mark.asyncio
async def test_attention_requires_explicit_approval(
    tmp_path: Path,
) -> None:
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    calls = Counter()

    def stage(_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NeedsAttentionStepError("identity review")
        return {"approved": True}

    hooks = OrchestratorHooks(plan_story=stage)
    await runner(repo, hooks).execute(
        run_id, input_payload={}, step_inputs={}, lease_owner="owner1",
    )
    assert repo.get_run(run_id)["status"] == "NEEDS_ATTENTION"
    with pytest.raises(DurableRunStateError, match="reconciliation"):
        await runner(repo, hooks).execute(
            run_id, input_payload={}, step_inputs={}, lease_owner="owner2",
        )
    final = await runner(repo, hooks).execute(
        run_id, input_payload={}, step_inputs={},
        lease_owner="owner2", approve_needs_attention_retry=True,
    )
    assert final.machine.state.value == "COMPLETED"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_interrupted_old_executor_fenced_and_recovery_needs_approval(
    tmp_path: Path,
) -> None:
    db = work(tmp_path)

    class Clock:
        moment = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def __call__(self):
            return self.moment

    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = start(repo)
    repo.acquire_lease(
        work_id="work_246", lease_owner="old_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=run_id,
    )
    repo.transition_run(run_id, expected_status="PENDING",
                        new_status="RUNNING", lease_owner="old_owner")
    step = repo.create_step(
        run_id=run_id, step_key="validate_input",
        input_fingerprint="old", lease_owner="old_owner",
    )
    repo.start_step(
        step["id"], input_fingerprint="old", lease_owner="old_owner",
    )
    clock.moment += timedelta(seconds=11)
    fresh = runner(DurableRunRepository(db, clock=clock), OrchestratorHooks())
    with pytest.raises(DurableRunStateError, match="reconciliation"):
        await fresh.execute(
            run_id, input_payload={}, step_inputs={},
            lease_owner="new_owner", reclaim_expired_owner="old_owner",
        )
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    assert repo.get_step(step["id"])["status"] == "INTERRUPTED"
    assert repo.list_step_attempts(step["id"])[0]["status"] == "INTERRUPTED"
    with pytest.raises(WorkLeaseConflictError):
        repo.finish_step(
            step["id"], status="COMPLETED", lease_owner="old_owner",
        )
    result = await fresh.execute(
        run_id, input_payload={}, step_inputs={},
        lease_owner="resumed_owner", approve_interrupted_retry=True,
    )
    assert result.machine.state.value == "COMPLETED"
    assert repo.get_step(step["id"])["attempt_count"] == 2
    assert [r["status"] for r in repo.list_step_attempts(step["id"])] == [
        "INTERRUPTED", "COMPLETED",
    ]


@pytest.mark.asyncio
async def test_non_serializable_hook_result_fails_closed(tmp_path: Path) -> None:
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    result = await runner(repo, OrchestratorHooks(
        generate_panels=lambda _: object(),
    )).execute(run_id, input_payload={}, step_inputs={},
               lease_owner="worker")
    assert result.machine.state.value == "FAILED_PANEL_GENERATION"
    assert repo.get_run(run_id)["status"] == "FAILED_TERMINAL"
    step = next(
        x for x in repo.list_steps(run_id) if x["step_key"] == "generate_panels"
    )
    assert step["status"] == "FAILED_TERMINAL"
    assert step["output_json"] == "{}"


@pytest.mark.asyncio
async def test_durable_run_requires_matching_work_and_valid_lease(tmp_path: Path) -> None:
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    foreign_id = repo.create_run(
        run_kind="AUTOPILOT", scope_type="WORK", scope_id="foreign",
        requested_by="test", input_fingerprint="x",
    )["id"]
    with pytest.raises(DurableRunStateError, match="owned"):
        await runner(repo, OrchestratorHooks()).execute(
            foreign_id, input_payload={}, step_inputs={},
        )
    assert repo.inspect_lease("work_246") is None


def test_append_only_step_attempt_receipts_and_foreign_keys(tmp_path: Path) -> None:
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    repo.transition_run(run_id, expected_status="PENDING", new_status="RUNNING")
    step = repo.create_step(
        run_id=run_id, step_key="one", input_fingerprint="v1",
    )
    repo.start_step(step["id"], input_fingerprint="v1")
    repo.finish_step(step["id"], status="FAILED_RETRYABLE")
    repo.start_step(step["id"], input_fingerprint="v1")
    repo.finish_step(step["id"], status="COMPLETED",
                     output={"value": {"x": 1}})
    attempts = repo.list_step_attempts(step["id"])
    assert len(attempts) == 2
    assert attempts[0]["status"] == "FAILED_RETRYABLE"
    assert attempts[1]["status"] == "COMPLETED"
    assert attempts[0]["started_at"] <= attempts[0]["finished_at"]
    assert attempts[0]["id"] != attempts[1]["id"]
    with repository_read(db) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT COUNT(*) FROM run_step_attempts WHERE run_step_id = ?",
            (step["id"],),
        ).fetchone()[0] == 2


@pytest.mark.asyncio
async def test_long_running_hook_renews_the_same_work_lease(
    tmp_path: Path,
) -> None:
    db = work(tmp_path)

    class Clock:
        moment = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def __call__(self):
            return self.moment

    clock = Clock()
    repo = DurableRunRepository(db, clock=clock)
    run_id = start(repo)
    observations = []

    def slow_render(_run):
        before = repo.inspect_lease("work_246")
        assert before is not None
        clock.moment += timedelta(seconds=4)
        time.sleep(2.3)
        after = repo.inspect_lease("work_246")
        assert after is not None
        observations.extend((before["expires_at"], after["expires_at"]))
        return {"rendered": True}

    result = await DurableAutopilotOrchestrator(
        repository=repo, work_id="work_246",
        hooks=OrchestratorHooks(render_pages=slow_render),
        lease_ttl_seconds=6,
    ).execute(
        run_id, input_payload={}, step_inputs={}, lease_owner="heartbeat_owner",
    )
    assert result.machine.state.value == "COMPLETED"
    assert len(observations) == 2
    assert observations[1] > observations[0]
    assert repo.inspect_lease("work_246") is None



@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_sync_hook_lease_and_heartbeat_until_exit(
    tmp_path: Path,
) -> None:
    """A canceled HTTP/task owner must not release a still-running sync hook."""
    db = work(tmp_path)

    class Clock:
        moment = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def __call__(self):
            return self.moment

    clock = Clock()
    owner_repo = DurableRunRepository(db, clock=clock)
    competing_repo = DurableRunRepository(db, clock=clock)
    run_id = start(owner_repo)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    effects = tmp_path / "canceled-hook-effects.txt"
    calls: Counter[str] = Counter()

    def blocked_sync_hook(_run):
        calls["old"] += 1
        entered.set()
        try:
            assert release.wait(timeout=20), "blocked sync hook never released"
            effects.write_text("old worker completed", encoding="utf-8")
            return {"effect": "old"}
        finally:
            finished.set()

    first = DurableAutopilotOrchestrator(
        repository=owner_repo, work_id="work_246",
        hooks=OrchestratorHooks(generate_panels=blocked_sync_hook),
        lease_ttl_seconds=3,
    )
    task = asyncio.create_task(first.execute(
        run_id, input_payload={"prompt": "same"},
        step_inputs={}, lease_owner="canceling_owner",
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        running = next(
            step for step in competing_repo.list_steps(run_id)
            if step["step_key"] == "generate_panels"
        )
        assert running["status"] == "RUNNING"
        assert running["attempt_count"] == 1
        initial_lease = competing_repo.inspect_lease("work_246")
        assert initial_lease is not None
        assert initial_lease["lease_owner"] == "canceling_owner"

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "repeat cancellation detached the hook worker"
        assert not finished.is_set()
        assert competing_repo.get_run(run_id)["status"] == "RUNNING"
        assert competing_repo.get_step(running["id"])["status"] == "RUNNING"
        assert competing_repo.list_step_attempts(running["id"])[0]["status"] == "RUNNING"

        # While a canceled owner is draining the OS thread, its heartbeat
        # must keep refreshing the same exclusive Work lease.
        clock.moment += timedelta(seconds=2)
        renewed = None
        for _ in range(160):
            renewed = competing_repo.inspect_lease("work_246")
            if renewed is not None and renewed["expires_at"] > initial_lease["expires_at"]:
                break
            await asyncio.sleep(0.05)
        assert renewed is not None and renewed["expires_at"] > initial_lease["expires_at"]
        assert renewed["expired"] is False
        assert not task.done()
        assert competing_repo.get_run(run_id)["status"] == "RUNNING"

        with pytest.raises(WorkLeaseConflictError):
            await DurableAutopilotOrchestrator(
                repository=competing_repo, work_id="work_246",
                hooks=OrchestratorHooks(),
            ).execute(
                run_id, input_payload={"prompt": "same"}, step_inputs={},
                lease_owner="another_owner",
                reclaim_expired_owner="canceling_owner",
                approve_interrupted_retry=True,
            )
        assert calls["old"] == 1
    finally:
        release.set()
        # The task remains canceled, even though the hook's side effects
        # must finish under its original owner before the lease is released.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20)

    assert finished.is_set()
    assert effects.read_text(encoding="utf-8") == "old worker completed"
    assert competing_repo.inspect_lease("work_246") is None
    assert competing_repo.get_run(run_id)["status"] == "INTERRUPTED"
    step = competing_repo.get_step(running["id"])
    assert step["status"] == "INTERRUPTED"
    assert step["attempt_count"] == 1
    assert [a["status"] for a in competing_repo.list_step_attempts(step["id"])] == [
        "INTERRUPTED",
    ]

    # A new repository/app is still denied blind replay after the worker has
    # exited, and an explicitly approved resume starts a new tracked attempt.
    recovered = DurableAutopilotOrchestrator(
        repository=competing_repo, work_id="work_246",
        hooks=OrchestratorHooks(generate_panels=lambda _: {"effect": "new"}),
    )
    with pytest.raises(DurableRunStateError, match="reconciliation"):
        await recovered.execute(
            run_id, input_payload={"prompt": "same"}, step_inputs={},
            lease_owner="new_owner",
        )
    result = await recovered.execute(
        run_id, input_payload={"prompt": "same"}, step_inputs={},
        lease_owner="new_owner", approve_interrupted_retry=True,
    )
    assert result.machine.state.value == "COMPLETED"
    assert competing_repo.get_run(run_id)["status"] == "COMPLETED"
    assert competing_repo.get_step(step["id"])["attempt_count"] == 2
    assert [a["status"] for a in competing_repo.list_step_attempts(step["id"])] == [
        "INTERRUPTED", "COMPLETED",
    ]
    assert calls["old"] == 1
    assert competing_repo.inspect_lease("work_246") is None


@pytest.mark.asyncio
async def test_canceled_sync_hook_exception_is_drained_without_orphaned_attempt(
    tmp_path: Path,
) -> None:
    """A late hook failure cannot replace cancellation or leak its lease."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def sync_hook(_):
        entered.set()
        try:
            assert release.wait(timeout=20)
            raise RuntimeError("hook failed after parent cancellation")
        finally:
            finished.set()

    orchestrator = runner(repo, OrchestratorHooks(plan_story=sync_hook))
    task = asyncio.create_task(orchestrator.execute(
        run_id, input_payload={}, step_inputs={}, lease_owner="error_owner",
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert repo.inspect_lease("work_246") is not None
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=20)
    assert finished.is_set()
    assert repo.inspect_lease("work_246") is None
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    step = next(s for s in repo.list_steps(run_id) if s["step_key"] == "plan_story")
    assert step["status"] == "INTERRUPTED"
    assert [a["status"] for a in repo.list_step_attempts(step["id"])] == [
        "INTERRUPTED",
    ]


@pytest.mark.asyncio
async def test_cancelled_async_hook_preserves_interrupted_receipt(
    tmp_path: Path,
) -> None:
    """Native async hooks still cancel without waiting for a thread."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def async_hook(_):
        entered.set()
        await blocked.wait()

    task = asyncio.create_task(runner(
        repo, OrchestratorHooks(plan_story=async_hook),
    ).execute(run_id, input_payload={}, step_inputs={}, lease_owner="async_owner"))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert repo.inspect_lease("work_246") is None
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    step = next(s for s in repo.list_steps(run_id) if s["step_key"] == "plan_story")
    assert step["status"] == "INTERRUPTED"
    assert repo.list_step_attempts(step["id"])[0]["status"] == "INTERRUPTED"
