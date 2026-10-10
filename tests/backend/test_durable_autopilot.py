"""GPU-free Work DB -> legacy hook bridge tests for Issue #246."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from manga_autopilot.repositories.durable_runs import (
    DurableRunRepository,
    DurableRunStateError,
    WorkLeaseConflictError,
)
from manga_autopilot.services.autopilot import Orchestrator, OrchestratorHooks
from manga_autopilot.services.durable_autopilot import (
    DurableAutopilotOrchestrator,
    DurableHeartbeatLostError,
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
        allow_omitted_hooks=True,
        repository=repo, work_id="work_246", hooks=hooks,
    )


# Allow slow SQLite stage setup on loaded Windows runners; the full
# Autopilot pipeline persists 13 preceding stages before Finalization begins.
# This is strictly the *entry/setup* deadline, not the heartbeat-loss deadline.
_FINALIZATION_SETUP_TIMEOUT_SECONDS = 90


async def _wait_for_thread_signal(
    event: threading.Event, *, timeout: float,
    task: asyncio.Task | None = None,
) -> bool:
    """Observe a sync worker without consuming its default-executor slots.

    asyncio.to_thread(event.wait, timeout) can starve the actual synchronous
    finalizer when the event-loop default executor is saturated. Polling the
    thread-safe Event from the loop preserves the bounded wait, and checking
    task.done() surfaces upstream early exits without waiting the full deadline.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if task is not None and task.done():
            return event.is_set()
        remaining = deadline - loop.time()
        if remaining <= 0:
            return event.is_set()
        await asyncio.sleep(min(0.01, remaining))
    return True


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
@pytest.mark.parametrize("foreign_kind", ["EXPORT", "QA"])
async def test_foreign_run_kind_rejected_without_durable_side_effects(
    tmp_path: Path, foreign_kind: str,
) -> None:
    """An in-Work generic Run is not a valid durable AUTOPILOT invocation."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = repo.create_run(
        run_kind=foreign_kind, scope_type="WORK", scope_id="work_246",
        requested_by="test", input_fingerprint="foreign:v1",
    )["id"]
    original = repo.get_run(run_id)
    called: list[str] = []

    def hook(_):
        called.append("validate_input")
        return {"unexpected": "effect"}

    orchestrator = DurableAutopilotOrchestrator(
        repository=repo, work_id="work_246",
        hooks=OrchestratorHooks(validate_input=hook),
        allow_omitted_hooks=True,
    )
    with pytest.raises(DurableRunStateError, match="AUTOPILOT"):
        await orchestrator.execute(
            run_id, input_payload={}, step_inputs={},
            lease_owner=f"foreign_{foreign_kind.lower()}",
        )

    assert repo.get_run(run_id) == original
    assert repo.list_steps(run_id) == []
    assert repo.inspect_lease("work_246") is None
    assert called == []


@pytest.mark.asyncio
async def test_autopilot_kind_runs_all_required_hooks(
    tmp_path: Path,
) -> None:
    """Do not reject legitimate AUTOPILOT Runs or change execution receipts."""
    repo = DurableRunRepository(work(tmp_path))
    run_id = start(repo)
    called: list[str] = []

    async def hook(_):
        called.append("executed")
        return {"real_hook_invoked": True}

    hooks = OrchestratorHooks(**{
        name: hook for name in OrchestratorHooks.__dataclass_fields__
    })
    result = await DurableAutopilotOrchestrator(
        repository=repo, work_id="work_246", hooks=hooks,
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="correct_autopilot_kind")

    assert result.machine.state.value == "COMPLETED"
    assert repo.get_run(run_id)["status"] == "COMPLETED"
    assert len(repo.list_steps(run_id)) == len(called) == len(
        OrchestratorHooks.__dataclass_fields__,
    )
    assert all(
        json.loads(step["output_json"]) == {
            "value": {"real_hook_invoked": True},
            "execution": "EXECUTED",
        }
        for step in repo.list_steps(run_id)
    )
    assert repo.inspect_lease("work_246") is None


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
    entered = threading.Event()
    release = threading.Event()

    def slow_render(_run):
        entered.set()
        assert release.wait(timeout=30), "heartbeat test did not release worker"
        return {"rendered": True}

    task = asyncio.create_task(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=repo, work_id="work_246",
        hooks=OrchestratorHooks(render_pages=slow_render),
        lease_ttl_seconds=6,
    ).execute(
        run_id, input_payload={}, step_inputs={}, lease_owner="heartbeat_owner",
    ))
    try:
        for _ in range(600):
            if entered.is_set():
                break
            await asyncio.sleep(0.05)
        assert entered.is_set(), "render thread never started"
        before = repo.inspect_lease("work_246")
        assert before is not None
        clock.moment += timedelta(seconds=4)
        after = None
        for _ in range(600):
            after = repo.inspect_lease("work_246")
            if after is not None and after["expires_at"] > before["expires_at"]:
                break
            await asyncio.sleep(0.05)
        assert after is not None
        assert after["expires_at"] > before["expires_at"]
        assert not task.done()
    finally:
        release.set()
    result = await asyncio.wait_for(task, timeout=30)
    assert result.machine.state.value == "COMPLETED"
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
        allow_omitted_hooks=True,
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
        initial_step_heartbeat = running["heartbeat_at"]
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
        draining_step = competing_repo.get_step(running["id"])
        assert draining_step["status"] == "RUNNING"
        assert draining_step["heartbeat_at"] > initial_step_heartbeat
        assert competing_repo.list_step_attempts(running["id"])[0]["status"] == "RUNNING"
        assert not task.done()
        assert competing_repo.get_run(run_id)["status"] == "RUNNING"

        with pytest.raises(WorkLeaseConflictError):
            await DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
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
        allow_omitted_hooks=True,
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



@pytest.mark.asyncio
async def test_failed_work_lease_heartbeat_drains_sync_hook_before_interrupting(
    tmp_path: Path,
) -> None:
    """Guardian failure races a running thread without publishing false success."""
    db = work(tmp_path)
    failed = threading.Event()
    started = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    effects = tmp_path / "heartbeat-effect.txt"

    class BrokenRenewalRepository(DurableRunRepository):
        def heartbeat_lease(self, *, work_id, lease_owner, ttl_seconds):
            # Under concurrent Windows CI load, the guardian can tick
            # before the sync thread starts; inject loss only IN-FLIGHT.
            if not started.is_set():
                return super().heartbeat_lease(
                    work_id=work_id, lease_owner=lease_owner,
                    ttl_seconds=ttl_seconds,
                )
            failed.set()
            raise OSError("injected lease renewal IO failure")

    owner_repo = BrokenRenewalRepository(db)
    competitor = DurableRunRepository(db)
    run_id = start(owner_repo)

    def slow_generation(_run):
        started.set()
        try:
            assert release.wait(timeout=20), "sync hook never released"
            effects.write_text("side effect completed", encoding="utf-8")
            return {"candidate": "must_not_be_committed"}
        finally:
            worker_finished.set()

    task = asyncio.create_task(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=owner_repo,
        work_id="work_246",
        hooks=OrchestratorHooks(generate_panels=slow_generation),
        lease_ttl_seconds=3,
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="failed_guardian"))
    try:
        for _ in range(600):
            if started.is_set():
                break
            await asyncio.sleep(0.05)
        assert started.is_set(), "generation thread never started"
        step = next(
            x for x in competitor.list_steps(run_id)
            if x["step_key"] == "generate_panels"
        )
        for _ in range(600):
            if failed.is_set():
                break
            await asyncio.sleep(0.05)
        assert failed.is_set(), "injected guardian failure not observed"
        await asyncio.sleep(0.05)
        assert not task.done(), "guardian failure detached the local OS worker"
        assert not worker_finished.is_set()
        assert competitor.get_run(run_id)["status"] == "RUNNING"
        assert competitor.get_step(step["id"])["status"] == "RUNNING"
        assert competitor.inspect_lease("work_246")["lease_owner"] == "failed_guardian"
        with pytest.raises(WorkLeaseConflictError):
            await runner(competitor, OrchestratorHooks()).execute(
                run_id, input_payload={}, step_inputs={},
                lease_owner="competing_owner", approve_interrupted_retry=True,
            )
    finally:
        release.set()

    with pytest.raises(DurableHeartbeatLostError, match="heartbeat"):
        await asyncio.wait_for(task, timeout=20)
    assert worker_finished.is_set()
    assert effects.read_text(encoding="utf-8") == "side effect completed"
    assert competitor.inspect_lease("work_246") is None
    assert competitor.get_run(run_id)["status"] == "INTERRUPTED"
    step = competitor.get_step(step["id"])
    assert step["status"] == "INTERRUPTED"
    assert step["output_json"] == "{}"
    attempts = competitor.list_step_attempts(step["id"])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "INTERRUPTED"
    assert "lease_heartbeat_lost" in attempts[0]["error_json"]

    clean = runner(
        competitor, OrchestratorHooks(generate_panels=lambda _: {"fixed": True}),
    )
    with pytest.raises(DurableRunStateError, match="reconciliation"):
        await clean.execute(run_id, input_payload={}, step_inputs={},
                            lease_owner="new_owner")
    resumed = await clean.execute(
        run_id, input_payload={}, step_inputs={},
        lease_owner="new_owner", approve_interrupted_retry=True,
    )
    assert resumed.machine.state.value == "COMPLETED"
    assert [x["status"] for x in competitor.list_step_attempts(step["id"])] == [
        "INTERRUPTED", "COMPLETED",
    ]


@pytest.mark.asyncio
async def test_expired_guardian_keeps_reclaim_proof_and_never_commits_hook_output(
    tmp_path: Path,
) -> None:
    """A clock jump cannot silently erase the expired old-owner token."""
    db = work(tmp_path)

    class Clock:
        moment = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def __call__(self):
            return self.moment

    clock = Clock()
    old = DurableRunRepository(db, clock=clock)
    fresh = DurableRunRepository(db, clock=clock)
    run_id = start(old)
    started = threading.Event()
    release = threading.Event()
    ended = threading.Event()

    def expired_hook(_):
        started.set()
        try:
            assert release.wait(timeout=20)
            return {"untrusted": "late"}
        finally:
            ended.set()

    task = asyncio.create_task(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=old, work_id="work_246",
        hooks=OrchestratorHooks(generate_panels=expired_hook),
        lease_ttl_seconds=3,
    ).execute(run_id, input_payload={}, step_inputs={}, lease_owner="expired_owner"))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        clock.moment += timedelta(seconds=4)
        # The guardian's first renewal must fail at the one-second tick.
        # Use portable task inspection (Python 3.10+), not Task.cancelling().
        # The hook remains blocked while the owner detects lease expiry.
        await asyncio.sleep(1.2)
        assert fresh.inspect_lease("work_246")["expired"]
        assert not task.done(), "expired owner detached its in-flight sync hook"
        assert not ended.is_set()
    finally:
        release.set()

    with pytest.raises(DurableHeartbeatLostError):
        await asyncio.wait_for(task, timeout=20)
    assert ended.is_set()
    old_step = next(
        x for x in fresh.list_steps(run_id)
        if x["step_key"] == "generate_panels"
    )
    assert old_step["status"] == "RUNNING"
    assert old_step["output_json"] == "{}"
    assert fresh.get_run(run_id)["status"] == "RUNNING"
    # An expired owner cannot delete the token and permit blind acquisition.
    lease = fresh.inspect_lease("work_246")
    assert lease is not None and lease["expired"]
    assert lease["lease_owner"] == "expired_owner"
    with pytest.raises(WorkLeaseConflictError, match="explicit"):
        await runner(fresh, OrchestratorHooks()).execute(
            run_id, input_payload={}, step_inputs={}, lease_owner="new_owner",
        )
    with pytest.raises(DurableRunStateError, match="reconciliation"):
        await runner(fresh, OrchestratorHooks()).execute(
            run_id, input_payload={}, step_inputs={}, lease_owner="new_owner",
            reclaim_expired_owner="expired_owner",
        )
    assert fresh.get_run(run_id)["status"] == "INTERRUPTED"
    assert fresh.get_step(old_step["id"])["status"] == "INTERRUPTED"
    assert fresh.list_step_attempts(old_step["id"])[0]["status"] == "INTERRUPTED"

    result = await runner(fresh, OrchestratorHooks(
        generate_panels=lambda _: {"trusted": "explicit_replay"},
    )).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="approved_owner", approve_interrupted_retry=True)
    assert result.machine.state.value == "COMPLETED"
    assert [a["status"] for a in fresh.list_step_attempts(old_step["id"])] == [
        "INTERRUPTED", "COMPLETED",
    ]


@pytest.mark.asyncio
async def test_lease_guardian_failure_cancels_native_async_hook(
    tmp_path: Path,
) -> None:
    """No async stage continues silently after renewal fails."""
    db = work(tmp_path)
    failed = threading.Event()
    started = asyncio.Event()
    hook_cancelled = asyncio.Event()

    class BrokenRenewalRepository(DurableRunRepository):
        def heartbeat_lease(self, *, work_id, lease_owner, ttl_seconds):
            failed.set()
            raise OSError("injected transient DB failure")

    repo = BrokenRenewalRepository(db)
    run_id = start(repo)

    async def suspended(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            hook_cancelled.set()

    task = asyncio.create_task(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=repo, work_id="work_246",
        hooks=OrchestratorHooks(plan_story=suspended),
        lease_ttl_seconds=3,
    ).execute(run_id, input_payload={}, step_inputs={}, lease_owner="async_guardian"))
    await asyncio.wait_for(started.wait(), timeout=10)
    assert await asyncio.to_thread(failed.wait, 10)
    with pytest.raises(DurableHeartbeatLostError):
        await asyncio.wait_for(task, timeout=20)
    assert hook_cancelled.is_set()
    step = next(s for s in repo.list_steps(run_id) if s["step_key"] == "plan_story")
    assert step["status"] == "INTERRUPTED"
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    assert repo.inspect_lease("work_246") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [-1, 0, 1, 2, True, 3.0])
async def test_unsafe_lease_heartbeat_period_is_rejected_before_mutation(
    tmp_path: Path, ttl,
) -> None:
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    with pytest.raises(ValueError, match="lease_ttl_seconds"):
        await DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
            repository=repo, work_id="work_246",
            lease_ttl_seconds=ttl,
        ).execute(run_id, input_payload={}, step_inputs={})
    assert repo.inspect_lease("work_246") is None
    assert repo.get_run(run_id)["status"] == "PENDING"



@pytest.mark.asyncio
async def test_async_stage_renews_step_run_and_work_heartbeats_together(
    tmp_path: Path,
) -> None:
    """An independent DB observer sees a live RunStep, not a stale start time."""
    db = work(tmp_path)

    class Clock:
        moment = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def __call__(self):
            return self.moment

    clock = Clock()
    owning_repo = DurableRunRepository(db, clock=clock)
    observer = DurableRunRepository(db, clock=clock)
    run_id = start(owning_repo)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_async_hook(_):
        entered.set()
        await release.wait()
        return {"finished": True}

    task = asyncio.create_task(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=owning_repo, work_id="work_246",
        hooks=OrchestratorHooks(plan_story=blocked_async_hook),
        lease_ttl_seconds=3,
    ).execute(run_id, input_payload={}, step_inputs={}, lease_owner="step_owner"))

    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        step = next(
            x for x in observer.list_steps(run_id)
            if x["step_key"] == "plan_story"
        )
        assert step["status"] == "RUNNING"
        assert step["attempt_count"] == 1
        started_heartbeat = step["heartbeat_at"]
        run_heartbeat = observer.get_run(run_id)["heartbeat_at"]
        lease = observer.inspect_lease("work_246")
        assert lease is not None
        assert lease["lease_owner"] == "step_owner"
        before_attempt = observer.list_step_attempts(step["id"])
        assert [r["status"] for r in before_attempt] == ["RUNNING"]

        clock.moment += timedelta(seconds=1)
        advanced = None
        for _ in range(160):
            advanced = observer.get_step(step["id"])
            if advanced["heartbeat_at"] > started_heartbeat:
                break
            await asyncio.sleep(0.05)

        assert advanced is not None
        assert advanced["status"] == "RUNNING"
        assert advanced["heartbeat_at"] > started_heartbeat
        assert observer.get_run(run_id)["heartbeat_at"] > run_heartbeat
        renewed = observer.inspect_lease("work_246")
        assert renewed is not None
        assert renewed["heartbeat_at"] > lease["heartbeat_at"]
        assert renewed["expired"] is False
        assert observer.list_step_attempts(step["id"]) == before_attempt
        assert not task.done()
    finally:
        release.set()

    result = await asyncio.wait_for(task, timeout=20)
    assert result.machine.state.value == "COMPLETED"
    finished = observer.get_step(step["id"])
    assert finished["status"] == "COMPLETED"
    assert finished["attempt_count"] == 1
    assert observer.get_run(run_id)["status"] == "COMPLETED"
    assert observer.inspect_lease("work_246") is None
    assert [r["status"] for r in observer.list_step_attempts(step["id"])] == [
        "COMPLETED",
    ]
    # There is no finalizer or unowned heartbeat after completion.
    await asyncio.sleep(0.05)
    assert observer.get_step(step["id"])["heartbeat_at"] == finished["heartbeat_at"]


@pytest.mark.asyncio
async def test_failed_step_heartbeat_stops_guardian_and_does_not_claim_success(
    tmp_path: Path,
) -> None:
    """Telemetry persistence failure is a lost guardian, not silent success."""
    db = work(tmp_path)
    attempted = threading.Event()
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    class FailStepHeartbeatRepository(DurableRunRepository):
        def heartbeat_step(self, step_id, *, lease_owner):
            # Fault starts only once the local worker has entered its hook.
            if not started.is_set():
                return super().heartbeat_step(step_id, lease_owner=lease_owner)
            attempted.set()
            raise OSError("injected RunStep heartbeat storage failure")

    owner = FailStepHeartbeatRepository(db)
    observer = DurableRunRepository(db)
    run_id = start(owner)

    def blocked_sync_hook(_):
        started.set()
        try:
            assert release.wait(timeout=20)
            return {"should_not_commit": True}
        finally:
            completed.set()

    task = asyncio.create_task(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=owner, work_id="work_246",
        hooks=OrchestratorHooks(generate_panels=blocked_sync_hook),
        lease_ttl_seconds=3,
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="failed_step_heartbeat"))
    try:
        for _ in range(600):
            if started.is_set():
                break
            await asyncio.sleep(0.05)
        assert started.is_set(), "generation thread never started"
        for _ in range(600):
            if attempted.is_set():
                break
            await asyncio.sleep(0.05)
        assert attempted.is_set(), "injected Step heartbeat failure not observed"
        await asyncio.sleep(0.05)
        assert not task.done()
        assert not completed.is_set()
        running_step = next(
            x for x in observer.list_steps(run_id)
            if x["step_key"] == "generate_panels"
        )
        assert running_step["status"] == "RUNNING"
        assert observer.inspect_lease("work_246") is not None
    finally:
        release.set()

    with pytest.raises(DurableHeartbeatLostError):
        await asyncio.wait_for(task, timeout=20)
    assert completed.is_set()
    assert observer.inspect_lease("work_246") is None
    assert observer.get_run(run_id)["status"] == "INTERRUPTED"
    current = observer.get_step(running_step["id"])
    assert current["status"] == "INTERRUPTED"
    assert current["output_json"] == "{}"
    assert [r["status"] for r in observer.list_step_attempts(current["id"])] == [
        "INTERRUPTED",
    ]



def test_durable_finalization_keeps_run_and_lease_running_through_file_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe a blocked real project-root mirroring from another DB owner.

    The orchestrator has its own event loop on an OS thread; the test owner
    observes DB state independently even when the old implementation blocks
    its own event loop with legacy._finalize().
    """
    db = work(tmp_path)
    root = tmp_path / "project"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "panel.png").write_bytes(b"rendered-panel")
    (root / "manifest.json").write_text('{"pages":[]}', encoding="utf-8")

    class Clock:
        moment = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def __call__(self):
            return self.moment

    clock = Clock()
    owner = DurableRunRepository(db, clock=clock)
    observer = DurableRunRepository(db, clock=clock)
    run_id = start(owner)
    entered = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}
    original_finalize = Orchestrator._finalize
    caller_thread = "durable-finalize-orchestrator"

    def blocking_finalize(self, run):
        entered.set()
        assert threading.current_thread().name != caller_thread, (
            "filesystem finalization must not block the heartbeat event loop"
        )
        assert release.wait(timeout=30), "finalize worker never released"
        return original_finalize(self, run)

    monkeypatch.setattr(Orchestrator, "_finalize", blocking_finalize)

    def execute_in_isolated_loop():
        try:
            result["run"] = asyncio.run(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
                repository=owner, work_id="work_246",
                hooks=OrchestratorHooks(),
                project_root=root, lease_ttl_seconds=3,
            ).execute(run_id, input_payload={}, step_inputs={},
                      lease_owner="finalizing_owner"))
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=execute_in_isolated_loop, name=caller_thread)
    thread.start()
    try:
        assert entered.wait(timeout=15), "legacy mirror never entered"
        last = next(
            step for step in observer.list_steps(run_id)
            if step["step_key"] == "finalize"
        )
        # No durable COMPLETED before the filesystem publication finishes.
        assert observer.get_run(run_id)["status"] == "RUNNING"
        assert last["status"] == "RUNNING"
        assert observer.list_step_attempts(last["id"])[0]["status"] == "RUNNING"
        before = observer.inspect_lease("work_246")
        assert before is not None and before["lease_owner"] == "finalizing_owner"
        clock.moment += timedelta(seconds=1)
        renewed = None
        for _ in range(300):
            renewed = observer.inspect_lease("work_246")
            if renewed and renewed["heartbeat_at"] > before["heartbeat_at"]:
                break
            threading.Event().wait(0.05)
        assert renewed is not None
        assert renewed["heartbeat_at"] > before["heartbeat_at"]
        assert renewed["expired"] is False
        # A guardian cycle uses separate short transactions for Lease, Run
        # and Step. A read observing the Lease update can precede the Step
        # heartbeat transaction; poll for the coordinated cycle to finish.
        for _ in range(300):
            if observer.get_step(last["id"])["heartbeat_at"] > last["heartbeat_at"]:
                break
            threading.Event().wait(0.05)
        assert observer.get_step(last["id"])["heartbeat_at"] > last["heartbeat_at"]
        assert observer.get_run(run_id)["status"] == "RUNNING"
        with pytest.raises(WorkLeaseConflictError, match="active"):
            observer.acquire_lease(
                work_id="work_246", lease_owner="competing_owner",
                lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
                run_id=run_id, reclaim_expired_owner="finalizing_owner",
            )
    finally:
        release.set()
        thread.join(timeout=30)

    assert not thread.is_alive(), "orchestrator retained a detached finalizer"
    assert "error" not in result, repr(result.get("error"))
    assert observer.get_run(run_id)["status"] == "COMPLETED"
    assert observer.get_step(last["id"])["status"] == "COMPLETED"
    assert [x["status"] for x in observer.list_step_attempts(last["id"])] == [
        "COMPLETED",
    ]
    assert observer.inspect_lease("work_246") is None
    assert (root / "runs" / run_id / "assets" / "panel.png").read_bytes() == (
        b"rendered-panel"
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_during_durable_finalization_drains_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = work(tmp_path)
    root = tmp_path / "project"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "panel.png").write_bytes(b"late-finalize")
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Clock:
        moment = datetime(2026, 10, 9, tzinfo=timezone.utc)

        def __call__(self):
            return self.moment

    clock = Clock()
    owner = DurableRunRepository(db, clock=clock)
    observer = DurableRunRepository(db, clock=clock)
    run_id = start(owner)
    original_finalize = Orchestrator._finalize

    def delayed_finalize(self, run):
        entered.set()
        try:
            assert release.wait(timeout=30)
            return original_finalize(self, run)
        finally:
            finished.set()

    monkeypatch.setattr(Orchestrator, "_finalize", delayed_finalize)
    task = asyncio.create_task(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=owner, work_id="work_246",
        project_root=root, lease_ttl_seconds=3,
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="canceled_finalizer"))
    try:
        assert await _wait_for_thread_signal(
            entered, timeout=_FINALIZATION_SETUP_TIMEOUT_SECONDS, task=task,
        )
        step = next(s for s in observer.list_steps(run_id)
                    if s["step_key"] == "finalize")
        before = observer.get_step(step["id"])["heartbeat_at"]
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()
        assert observer.get_run(run_id)["status"] == "RUNNING"
        assert observer.get_step(step["id"])["status"] == "RUNNING"
        clock.moment += timedelta(seconds=1)
        for _ in range(160):
            if observer.get_step(step["id"])["heartbeat_at"] > before:
                break
            await asyncio.sleep(0.05)
        assert observer.get_step(step["id"])["heartbeat_at"] > before
        assert observer.inspect_lease("work_246")["expired"] is False
        with pytest.raises(WorkLeaseConflictError):
            observer.acquire_lease(
                work_id="work_246", lease_owner="takeover",
                lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10,
                run_id=run_id, reclaim_expired_owner="canceled_finalizer",
            )
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)

    assert finished.is_set()
    assert observer.inspect_lease("work_246") is None
    assert observer.get_run(run_id)["status"] == "INTERRUPTED"
    assert observer.get_step(step["id"])["status"] == "INTERRUPTED"
    assert [x["status"] for x in observer.list_step_attempts(step["id"])] == [
        "INTERRUPTED",
    ]
    assert (root / "runs" / run_id / "assets" / "panel.png").is_file()
    # Publication could already have happened before cancellation; a second
    # attempt requires human reconciliation rather than automatic repetition.
    monkeypatch.setattr(Orchestrator, "_finalize", original_finalize)
    with pytest.raises(DurableRunStateError, match="reconciliation"):
        await DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
            repository=observer, work_id="work_246", project_root=root,
        ).execute(run_id, input_payload={}, step_inputs={},
                  lease_owner="fresh_owner")
    done = await DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=observer, work_id="work_246", project_root=root,
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="fresh_owner", approve_interrupted_retry=True)
    assert done.machine.state.value == "COMPLETED"
    assert observer.get_run(run_id)["status"] == "COMPLETED"
    assert [x["status"] for x in observer.list_step_attempts(step["id"])] == [
        "INTERRUPTED", "COMPLETED",
    ]
    assert observer.get_step(step["id"])["attempt_count"] == 2


@pytest.mark.asyncio
async def test_finalization_heartbeat_failure_drains_and_records_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = work(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    lost = threading.Event()

    class FailDuringFinalize(DurableRunRepository):
        def heartbeat_step(self, step_id, *, lease_owner):
            step = self.get_step(step_id)
            if step["step_key"] == "finalize" and entered.is_set():
                lost.set()
                raise OSError("injected finalization heartbeat failure")
            return super().heartbeat_step(step_id, lease_owner=lease_owner)

    # The test isolates a deliberately injected step heartbeat error, not
    # an unrelated wall-clock Work-lease expiry under slow Windows scheduling.
    # Real lease-expiry and takeover behavior has separate repository tests.
    class FrozenClock:
        def __call__(self) -> datetime:
            return datetime(2026, 10, 9, tzinfo=timezone.utc)

    clock = FrozenClock()
    owner = FailDuringFinalize(db, clock=clock)
    observer = DurableRunRepository(db, clock=clock)
    run_id = start(owner)
    original_finalize = Orchestrator._finalize

    def blocking_finalize(self, run):
        entered.set()
        try:
            assert release.wait(timeout=30)
            (root / "published.txt").write_text("late side effect", encoding="utf-8")
            return original_finalize(self, run)
        finally:
            exited.set()

    monkeypatch.setattr(Orchestrator, "_finalize", blocking_finalize)
    task = asyncio.create_task(DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=owner, work_id="work_246", project_root=root,
        lease_ttl_seconds=3,
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="lost_finalizer"))
    try:
        # A prior postmerge Windows CI failure showed export still RUNNING,
        # with a live lease and task when the old 15s setup wait expired.
        entered_in_time = await _wait_for_thread_signal(
            entered, timeout=_FINALIZATION_SETUP_TIMEOUT_SECONDS, task=task,
        )
        if not entered_in_time:
            steps = [
                (step["step_key"], step["status"])
                for step in observer.list_steps(run_id)
            ]
            task_error = (
                repr(task.exception())
                if task.done() and not task.cancelled() else None
            )
            pytest.fail(
                f"finalizer not entered; run={observer.get_run(run_id)['status']}, "
                f"steps={steps}, lease={observer.inspect_lease('work_246')}, "
                f"task_done={task.done()}, task_error={task_error}"
            )
        assert await _wait_for_thread_signal(lost, timeout=15, task=task)
        await asyncio.sleep(0.05)
        assert not task.done()
        assert not exited.is_set()
        last = next(s for s in observer.list_steps(run_id)
                    if s["step_key"] == "finalize")
        assert last["status"] == "RUNNING"
        assert observer.get_run(run_id)["status"] == "RUNNING"
        current_lease = observer.inspect_lease("work_246")
        assert current_lease["lease_owner"] == "lost_finalizer"
        assert current_lease["expired"] is False
    finally:
        release.set()

    with pytest.raises(DurableHeartbeatLostError):
        await asyncio.wait_for(task, timeout=30)
    assert exited.is_set()
    assert (root / "published.txt").read_text(encoding="utf-8") == "late side effect"
    assert observer.get_run(run_id)["status"] == "INTERRUPTED"
    assert observer.get_step(last["id"])["status"] == "INTERRUPTED"
    assert [x["status"] for x in observer.list_step_attempts(last["id"])] == [
        "INTERRUPTED",
    ]
    assert observer.inspect_lease("work_246") is None


@pytest.mark.asyncio
async def test_unexpected_finalization_error_has_no_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)

    def failed_finalize(self, run):
        raise RuntimeError("filesystem publication failed")

    monkeypatch.setattr(Orchestrator, "_finalize", failed_finalize)
    result = await DurableAutopilotOrchestrator(
        allow_omitted_hooks=True,
        repository=repo, work_id="work_246",
        project_root=tmp_path / "project",
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="failing_finalizer")
    assert result.machine.state.value.startswith("FAILED")
    assert repo.get_run(run_id)["status"] == "FAILED_TERMINAL"
    last = next(s for s in repo.list_steps(run_id) if s["step_key"] == "finalize")
    assert last["status"] == "FAILED_TERMINAL"
    assert [x["status"] for x in repo.list_step_attempts(last["id"])] == [
        "FAILED_TERMINAL",
    ]
    assert repo.inspect_lease("work_246") is None



@pytest.mark.asyncio
async def test_missing_required_durable_hooks_fail_closed_before_mutation(
    tmp_path: Path,
) -> None:
    """The default adapter must never claim to have generated a manga."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    with pytest.raises(DurableRunStateError, match="required.*hook"):
        await DurableAutopilotOrchestrator(
            repository=repo, work_id="work_246",
            hooks=OrchestratorHooks(validate_input=lambda _: {"ok": True}),
        ).execute(run_id, input_payload={}, step_inputs={},
                  lease_owner="required_owner")
    assert repo.get_run(run_id)["status"] == "PENDING"
    assert repo.list_steps(run_id) == []
    assert repo.inspect_lease("work_246") is None


@pytest.mark.asyncio
async def test_skeletal_mode_explicitly_records_omitted_vs_executed_none(
    tmp_path: Path,
) -> None:
    """Explicit omission is an audit receipt, never proof of GPU generation."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)

    async def validate(_):
        return None  # An actually invoked hook can legitimately return None.

    result = await DurableAutopilotOrchestrator(
        repository=repo, work_id="work_246",
        hooks=OrchestratorHooks(validate_input=validate),
        allow_omitted_hooks=True,
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="explicit_skeleton")
    assert result.machine.state.value == "COMPLETED"
    assert repo.get_run(run_id)["status"] == "COMPLETED"
    steps = {s["step_key"]: s for s in repo.list_steps(run_id)}
    assert json.loads(steps["validate_input"]["output_json"]) == {
        "value": None, "execution": "EXECUTED",
    }
    assert json.loads(steps["generate_panels"]["output_json"]) == {
        "value": None, "execution": "OMITTED",
    }
    assert json.loads(steps["export"]["output_json"]) == {
        "value": None, "execution": "OMITTED",
    }
    assert [a["status"] for a in repo.list_step_attempts(
        steps["generate_panels"]["id"],
    )] == ["COMPLETED"]
    assert any(e["kind"] == "step_omitted" for e in result.log)
    assert repo.inspect_lease("work_246") is None


@pytest.mark.asyncio
async def test_attaching_hook_after_omitted_stage_requires_new_run(
    tmp_path: Path,
) -> None:
    """Do not skip omitted output or auto-repeat downstream stochastic work."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    calls: Counter[str] = Counter()

    def generation(_):
        calls["generate"] += 1
        raise RetryableStepError("after omitted story")

    original = DurableAutopilotOrchestrator(
        repository=repo, work_id="work_246",
        hooks=OrchestratorHooks(generate_panels=generation),
        allow_omitted_hooks=True,
    )
    failed = await original.execute(
        run_id, input_payload={}, step_inputs={}, lease_owner="old_skeleton",
    )
    assert failed.machine.state.value.startswith("FAILED")
    assert repo.get_run(run_id)["status"] == "FAILED_RETRYABLE"
    story = next(s for s in repo.list_steps(run_id)
                 if s["step_key"] == "plan_story")
    before = repo.list_step_attempts(story["id"])
    assert json.loads(story["output_json"])["execution"] == "OMITTED"

    async def new_story(_):
        calls["story"] += 1
        return {"pages": 12}

    fresh = DurableAutopilotOrchestrator(
        repository=DurableRunRepository(db), work_id="work_246",
        hooks=OrchestratorHooks(
            plan_story=new_story, generate_panels=generation,
        ),
        allow_omitted_hooks=True,
    )
    with pytest.raises(DurableRunStateError, match="omitted.*new Run"):
        await fresh.execute(
            run_id, input_payload={}, step_inputs={},
            lease_owner="new_capability",
        )
    assert repo.get_run(run_id)["status"] == "FAILED_RETRYABLE"
    assert repo.inspect_lease("work_246") is None
    assert repo.list_step_attempts(story["id"]) == before
    assert calls == {"generate": 1}

    new_run = start(repo)
    fresh_ok = DurableAutopilotOrchestrator(
        repository=DurableRunRepository(db), work_id="work_246",
        hooks=OrchestratorHooks(plan_story=new_story),
        allow_omitted_hooks=True,
    )
    completed = await fresh_ok.execute(
        new_run, input_payload={}, step_inputs={},
        lease_owner="new_run",
    )
    assert completed.machine.state.value == "COMPLETED"
    assert calls["story"] == 1
    new_story_step = next(s for s in repo.list_steps(new_run)
                          if s["step_key"] == "plan_story")
    assert json.loads(new_story_step["output_json"]) == {
        "value": {"pages": 12}, "execution": "EXECUTED",
    }


@pytest.mark.asyncio
async def test_ambiguous_legacy_null_receipt_fails_closed_on_new_hook(
    tmp_path: Path,
) -> None:
    """Older completed-null receipts never prove a real hook ran."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)

    def stop(_):
        raise RetryableStepError("keep Run resumable")

    await DurableAutopilotOrchestrator(
        repository=repo, work_id="work_246",
        hooks=OrchestratorHooks(generate_panels=stop),
        allow_omitted_hooks=True,
    ).execute(run_id, input_payload={}, step_inputs={}, lease_owner="legacy")
    story = next(s for s in repo.list_steps(run_id)
                 if s["step_key"] == "plan_story")
    with repository_write(db) as conn:
        conn.execute(
            "UPDATE run_steps SET output_json = ? WHERE id = ?",
            ('{"value":null}', story["id"]),
        )
    old = repo.list_step_attempts(story["id"])
    with pytest.raises(DurableRunStateError, match="legacy.*new Run"):
        await DurableAutopilotOrchestrator(
            repository=DurableRunRepository(db), work_id="work_246",
            hooks=OrchestratorHooks(
                plan_story=lambda _: {"real": True},
                generate_panels=stop,
            ),
            allow_omitted_hooks=True,
        ).execute(run_id, input_payload={}, step_inputs={},
                  lease_owner="new_after_upgrade")
    assert repo.inspect_lease("work_246") is None
    assert repo.get_run(run_id)["status"] == "FAILED_RETRYABLE"
    assert repo.list_step_attempts(story["id"]) == old


@pytest.mark.asyncio
async def test_phase_c_audit_preflight_step_error_cannot_abandon_running_run(
    tmp_path: Path,
) -> None:
    """Unexpected replay-state rejection must retain explicit recovery evidence."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    repo.acquire_lease(
        work_id="work_246", lease_owner="initial_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="initial_owner",
    )
    step = repo.create_step(
        run_id=run_id, step_key="validate_input",
        input_fingerprint="previous:v1", lease_owner="initial_owner",
    )
    repo.start_step(
        step["id"], input_fingerprint="previous:v1",
        lease_owner="initial_owner",
    )
    repo.finish_step(
        step["id"], status="FAILED_TERMINAL",
        error={"message": "unreplayable"}, lease_owner="initial_owner",
    )
    repo.transition_run(
        run_id, expected_status="RUNNING", new_status="FAILED_RETRYABLE",
        lease_owner="initial_owner",
    )
    repo.release_lease(work_id="work_246", lease_owner="initial_owner")
    assert repo.inspect_lease("work_246") is None
    resumed = DurableRunRepository(db)
    with pytest.raises(DurableRunStateError, match="terminal failed step"):
        await runner(resumed, OrchestratorHooks()).execute(
            run_id, input_payload={}, step_inputs={}, lease_owner="new_owner",
        )
    # Failed mid-resume preflight must not allow a different Run to start
    # while old durable state still advertises RUNNING.
    latest = resumed.get_run(run_id)
    lease = resumed.inspect_lease("work_246")
    assert latest["status"] != "RUNNING" or lease is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("outside_stage_failure", ["cancel", "exception"])
async def test_early_orchestration_abort_keeps_lease_until_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside_stage_failure: str,
) -> None:
    """Pre-Step failures must not orphan RUNNING Runs or unblock new writers."""
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    calls = repo.list_steps

    def fail_after_run_started(requested_run_id: str):
        if repo.get_run(requested_run_id)["status"] == "RUNNING":
            if outside_stage_failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("pre-step validation crashed")
        return calls(requested_run_id)

    monkeypatch.setattr(repo, "list_steps", fail_after_run_started)
    error = (
        asyncio.CancelledError if outside_stage_failure == "cancel"
        else RuntimeError
    )
    with pytest.raises(error):
        await runner(repo, OrchestratorHooks()).execute(
            run_id, input_payload={}, step_inputs={},
            lease_owner="aborted_owner",
        )
    assert repo.get_run(run_id)["status"] == "RUNNING"
    lease = repo.inspect_lease("work_246")
    assert lease is not None
    assert lease["lease_owner"] == "aborted_owner"
    assert lease["run_id"] == run_id
    another = DurableRunRepository(db)
    with pytest.raises(WorkLeaseConflictError):
        another.acquire_lease(
            work_id="work_246", lease_owner="other_owner",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60,
        )


def test_issue388_finalizer_start_wait_must_not_starve_single_worker_executor() -> None:
    """An entry observer must not consume the sole worker needed by finalize.

    The old asyncio.to_thread(entered.wait, ...) consumed a worker
    from the same default pool as the synchronous _finalize bridge.
    This regression passes only if the observer does not take that worker.
    """
    from concurrent.futures import ThreadPoolExecutor

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        entered = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            loop.set_default_executor(pool)

            async def finalizer() -> None:
                await asyncio.to_thread(entered.set)

            task = asyncio.create_task(finalizer())
            try:
                # The observer must allow the sole worker to enter finalize.
                assert await _wait_for_thread_signal(entered, timeout=2, task=task)
            finally:
                # Allow the actual worker to finish before the test exits.
                await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_issue407_slow_durable_step_io_does_not_block_lease_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable RunStep SQLite operations must not starve their own guardian.

    Simulate a slow synchronous SQLite writer on the *real* durable execution
    path, then schedule a loop callback from a separate watcher thread. The
    watcher always releases the writer after a bounded period, even when the
    event loop is blocked. Only a callback serviced while that IO is pending
    proves that another short-TTL Work lease heartbeat can run.
    """
    db = work(tmp_path)
    repo = DurableRunRepository(db)
    run_id = start(repo)
    loop = asyncio.get_running_loop()
    started = threading.Event()
    release = threading.Event()
    pulse = threading.Event()
    observed: dict[str, bool] = {}
    original_start = DurableRunRepository.start_step

    def delayed_start(self, *args, **kwargs):
        started.set()
        if not release.wait(timeout=15):
            raise TimeoutError("audit-controlled RunStep SQLite delay not released")
        return original_start(self, *args, **kwargs)

    def independent_tick() -> None:
        if not started.wait(timeout=10):
            observed["loop_tick_during_step_io"] = False
            release.set()
            return
        try:
            loop.call_soon_threadsafe(pulse.set)
            observed["loop_tick_during_step_io"] = pulse.wait(timeout=3)
        finally:
            release.set()

    monkeypatch.setattr(DurableRunRepository, "start_step", delayed_start)
    watcher = threading.Thread(target=independent_tick, daemon=True)
    watcher.start()
    try:
        result = await runner(repo, OrchestratorHooks()).execute(
            run_id, input_payload={}, step_inputs={}, lease_owner="audit_worker",
        )
        assert result.machine.state.value == "COMPLETED"
    finally:
        release.set()
        await asyncio.to_thread(watcher.join, 10)
    assert not watcher.is_alive()
    assert observed["loop_tick_during_step_io"] is True, (
        "Durable Autopilot runs synchronous RunStep SQLite IO on its own "
        "heartbeat event loop, starving independent lease renewal callbacks"
    )
