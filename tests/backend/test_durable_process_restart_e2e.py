"""True cross-process restart/resume E2E against a shared v2 Work SQLite DB.

No GPU, live ComfyUI, remote API or sleeps. The child uses os._exit(83) to
simulate a hard kill while an actual persisted RunStep is RUNNING. A separate
Python interpreter creates a fresh aiohttp Application and Work repository.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manga_autopilot.repositories.durable_runs import DurableRunRepository
from manga_autopilot.storage import (
    bootstrap_work_database,
    create_work_commit,
    create_work_entity_revision,
    repository_read,
    repository_write,
)

_CHILD = Path(__file__).with_name("_durable_restart_worker.py")
_ROOT = Path(__file__).resolve().parents[2]


def _prepare_db(tmp_path: Path) -> tuple[Path, DurableRunRepository, str]:
    db = tmp_path / "work.sqlite3"
    bootstrap_work_database(db, work_id="work_247")
    with repository_write(db) as connection:
        commit = create_work_commit(
            connection,
            commit_id="work_247_init",
            actor_type="system",
            operation_type="create_work",
        )
        connection.execute(
            """INSERT INTO work_metadata (
                work_id, title, work_kind, language, reading_direction,
                status, current_commit_seq, current_revision,
                created_at, updated_at
            ) VALUES (?, ?, 'standalone', 'ja', 'RTL_TOP_TO_BOTTOM',
                      'DRAFT', ?, 1, ?, ?)""",
            ("work_247", "Work #247", commit.commit_seq,
             commit.created_at, commit.created_at),
        )
        create_work_entity_revision(
            connection,
            revision_id="work_247_revision",
            entity_type="work",
            entity_id="work_247",
            entity_revision=1,
            commit_seq=commit.commit_seq,
            change_kind="create",
            after_state={"title": "Work #247"},
        )
    repo = DurableRunRepository(db)
    run_id = repo.create_run(
        run_kind="AUTOPILOT",
        scope_type="WORK",
        scope_id="work_247",
        requested_by="process_restart_e2e",
        input_fingerprint="work_247_inputs",
    )["id"]
    return db, repo, run_id


def _invoke(
    *,
    db: Path,
    events: Path,
    run_id: str,
    mode: str,
    owner: str,
    generation_version: str = "v1",
    offset: int = 0,
    reclaim_owner: str | None = None,
    approve_interrupted: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(_CHILD),
        "--database", str(db),
        "--events", str(events),
        "--run-id", run_id,
        "--mode", mode,
        "--owner", owner,
        "--generation-version", generation_version,
        "--clock-offset", str(offset),
    ]
    if reclaim_owner is not None:
        command.extend(["--reclaim-owner", reclaim_owner])
    if approve_interrupted:
        command.append("--approve-interrupted")
    return subprocess.run(  # noqa: S603 - fixed interpreter and local fixture
        command,
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _result(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert process.returncode == 0, (
        f"unexpected child exit={process.returncode}\n"
        f"stdout={process.stdout}\nstderr={process.stderr}"
    )
    return json.loads(process.stdout.strip().splitlines()[-1])


def _events(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]



def test_new_python_process_cannot_take_unreconciled_cross_run_lease(
    tmp_path: Path,
) -> None:
    """Independent interpreter cannot transfer another active Run's receipts."""
    db, repo, old_id = _prepare_db(tmp_path)
    new_id = repo.create_run(
        run_kind="AUTOPILOT", scope_type="WORK", scope_id="work_247",
        requested_by="restart_check", input_fingerprint="new:v1",
    )["id"]
    old = DurableRunRepository(
        db, clock=lambda: datetime(2026, 10, 9, tzinfo=timezone.utc),
    )
    old.acquire_lease(
        work_id="work_247", lease_owner="old_owner",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=10, run_id=old_id,
    )
    old.transition_run(
        old_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="old_owner",
    )
    step = old.create_step(
        run_id=old_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="old_owner",
    )
    old.start_step(
        step["id"], input_fingerprint="v1", lease_owner="old_owner",
    )
    events = tmp_path / "cross-run-denied.jsonl"
    before = repo.list_step_attempts(step["id"])
    result = _result(_invoke(
        db=db, events=events, run_id=new_id, mode="normal",
        owner="new_owner", offset=20, reclaim_owner="old_owner",
    ))
    assert result["outcome"] == "rejected"
    assert result["type"] == "WorkLeaseConflictError"
    assert repo.get_run(old_id)["status"] == "RUNNING"
    assert repo.get_step(step["id"])["status"] == "RUNNING"
    assert repo.list_step_attempts(step["id"]) == before
    assert repo.get_run(new_id)["status"] == "PENDING"
    assert repo.inspect_lease("work_247")["lease_owner"] == "old_owner"
    assert not events.exists()

def test_foreign_run_kind_stays_pending_across_fresh_processes(
    tmp_path: Path,
) -> None:
    """New Python interpreters must not dispatch EXPORT as an Autopilot."""
    db, repo, _ = _prepare_db(tmp_path)
    run_id = repo.create_run(
        run_kind="EXPORT", scope_type="WORK", scope_id="work_247",
        requested_by="process_restart_e2e", input_fingerprint="export:v1",
    )["id"]
    initial = repo.get_run(run_id)
    events = tmp_path / "wrong-kind-events.jsonl"

    for owner in ("first_foreign_process", "second_foreign_process"):
        denied = _result(_invoke(
            db=db, events=events, run_id=run_id, mode="normal",
            owner=owner,
        ))
        assert denied["outcome"] == "rejected"
        assert denied["type"] == "DurableRunStateError"
        assert "AUTOPILOT" in denied["message"]
        assert repo.get_run(run_id) == initial
        assert repo.list_steps(run_id) == []
        assert repo.inspect_lease("work_247") is None
        assert not events.exists()



def test_hard_kill_and_fresh_application_resume_only_missing_step(
    tmp_path: Path,
) -> None:
    db, repo, run_id = _prepare_db(tmp_path)
    events = tmp_path / "events.jsonl"
    created_at = repo.get_run(run_id)["created_at"]

    first = _invoke(
        db=db, events=events, run_id=run_id, mode="crash_generation",
        owner="worker_before_crash",
    )
    assert first.returncode == 83, (
        f"expected a hard process exit: {first.stdout}\n{first.stderr}"
    )
    assert repo.get_run(run_id)["status"] == "RUNNING"
    before_steps = {s["step_key"]: s for s in repo.list_steps(run_id)}
    assert before_steps["validate_input"]["status"] == "COMPLETED"
    assert before_steps["plan_story"]["status"] == "COMPLETED"
    assert before_steps["generate_panels"]["status"] == "RUNNING"
    assert before_steps["generate_panels"]["attempt_count"] == 1
    assert repo.list_step_attempts(
        before_steps["generate_panels"]["id"]
    )[0]["status"] == "RUNNING"

    # A fresh process cannot silently steal an expired exclusive Work lease.
    denied = _result(_invoke(
        db=db, events=events, run_id=run_id, mode="normal",
        owner="unapproved_worker", offset=20,
    ))
    assert denied["outcome"] == "rejected"
    assert denied["type"] == "WorkLeaseConflictError"
    assert repo.get_run(run_id)["status"] == "RUNNING"
    assert repo.get_step(
        before_steps["generate_panels"]["id"]
    )["status"] == "RUNNING"

    # A new application explicitly recovers the old owner, marks the
    # unresolved attempt INTERRUPTED, and refuses a blind replay.
    inspected = _result(_invoke(
        db=db, events=events, run_id=run_id, mode="normal",
        owner="inspection_worker", offset=20,
        reclaim_owner="worker_before_crash",
    ))
    assert inspected["outcome"] == "rejected"
    assert inspected["type"] == "DurableRunStateError"
    assert "reconciliation" in inspected["message"]
    assert repo.get_run(run_id)["status"] == "INTERRUPTED"
    assert repo.get_step(
        before_steps["generate_panels"]["id"]
    )["status"] == "INTERRUPTED"
    assert repo.list_step_attempts(
        before_steps["generate_panels"]["id"]
    )[0]["status"] == "INTERRUPTED"
    assert repo.inspect_lease("work_247") is None

    # A third fresh app replays only the interrupted/missing stage and
    # rehydrates completed JSON outputs from SQLite.
    resumed = _result(_invoke(
        db=db, events=events, run_id=run_id, mode="normal",
        owner="new_worker", offset=20, approve_interrupted=True,
    ))
    assert resumed["outcome"] == "finished"
    assert resumed["state"] == "COMPLETED"
    assert resumed["durable_status"] == "COMPLETED"

    entries = _events(events)
    assert Counter(x["stage"] for x in entries) == {
        "validate_input": 1,
        "plan_story": 1,
        "generate_panels": 2,
        "render_pages": 1,
        "export": 1,
    }
    assert entries[0]["app_id"] != resumed["app_id"]
    assert repo.get_run(run_id)["created_at"] == created_at
    assert repo.get_run(run_id)["status"] == "COMPLETED"
    assert repo.inspect_lease("work_247") is None
    after_steps = {s["step_key"]: s for s in repo.list_steps(run_id)}
    assert set(before_steps).issubset(after_steps)
    assert set(after_steps) - set(before_steps) == {
        "qa_panels", "lettering", "render_pages", "export", "finalize",
    }
    for name in ("validate_input", "plan_story"):
        assert after_steps[name]["id"] == before_steps[name]["id"]
        assert after_steps[name]["attempt_count"] == 1
    assert after_steps["generate_panels"]["id"] == (
        before_steps["generate_panels"]["id"]
    )
    assert after_steps["generate_panels"]["attempt_count"] == 2
    assert [attempt["status"] for attempt in repo.list_step_attempts(
        after_steps["generate_panels"]["id"]
    )] == ["INTERRUPTED", "COMPLETED"]
    assert all(step["status"] == "COMPLETED" for step in after_steps.values())
    with repository_read(db) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT COUNT(*) FROM runs WHERE id = ?", (run_id,),
        ).fetchone()[0] == 1


def test_changed_stage_input_after_reopen_reexecutes_only_downstream(
    tmp_path: Path,
) -> None:
    db, repo, run_id = _prepare_db(tmp_path)
    events = tmp_path / "changed-input.jsonl"

    failed = _result(_invoke(
        db=db, events=events, run_id=run_id,
        mode="retryable_render_failure",
        owner="initial_worker", generation_version="v1",
    ))
    assert failed["outcome"] == "finished"
    assert failed["durable_status"] == "FAILED_RETRYABLE"
    before = {s["step_key"]: s for s in repo.list_steps(run_id)}
    assert before["generate_panels"]["status"] == "COMPLETED"
    assert before["render_pages"]["status"] == "FAILED_RETRYABLE"
    assert "export" not in before
    assert repo.inspect_lease("work_247") is None

    # A second separate interpreter sees a different generation fingerprint.
    # validate/story remain reusable; generate and descendants must re-run.
    succeeded = _result(_invoke(
        db=db, events=events, run_id=run_id, mode="normal",
        owner="changed_worker", generation_version="v2",
    ))
    assert succeeded["durable_status"] == "COMPLETED"
    entries = _events(events)
    counts = Counter(item["stage"] for item in entries)
    assert counts == {
        "validate_input": 1,
        "plan_story": 1,
        "generate_panels": 2,
        "render_pages": 2,
        "export": 1,
    }
    assert failed["app_id"] != succeeded["app_id"]
    assert [item["generation_version"] for item in entries
            if item["stage"] == "generate_panels"] == ["v1", "v2"]
    after = {s["step_key"]: s for s in repo.list_steps(run_id)}
    assert set(before).issubset(after)
    assert set(after) - set(before) == {"export", "finalize"}
    assert all(after[k]["id"] == before[k]["id"] for k in before)
    assert after["validate_input"]["attempt_count"] == 1
    assert after["plan_story"]["attempt_count"] == 1
    assert after["generate_panels"]["attempt_count"] == 2
    assert after["qa_panels"]["attempt_count"] == 2
    assert after["render_pages"]["attempt_count"] == 2
    assert after["export"]["status"] == "COMPLETED"
    assert [attempt["status"] for attempt in repo.list_step_attempts(
        after["generate_panels"]["id"]
    )] == ["COMPLETED", "COMPLETED"]
    assert [attempt["input_fingerprint"] for attempt
            in repo.list_step_attempts(after["generate_panels"]["id"])][0] != (
        repo.list_step_attempts(after["generate_panels"]["id"])[1][
            "input_fingerprint"
        ]
    )
    assert repo.get_run(run_id)["status"] == "COMPLETED"
    assert repo.inspect_lease("work_247") is None


def test_fresh_interpreter_cannot_release_live_running_run_lease(
    tmp_path: Path,
) -> None:
    """Cross-process owner token alone must not erase recovery evidence."""
    db, repo, run_id = _prepare_db(tmp_path)
    repo.acquire_lease(
        work_id="work_247", lease_owner="still_working",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=90, run_id=run_id,
    )
    repo.transition_run(
        run_id, expected_status="PENDING", new_status="RUNNING",
        lease_owner="still_working",
    )
    step_id = repo.create_step(
        run_id=run_id, step_key="generate_panels",
        input_fingerprint="v1", lease_owner="still_working",
    )["id"]
    repo.start_step(
        step_id, input_fingerprint="v1", lease_owner="still_working",
    )
    script = (
        "import sys\n"
        "from manga_autopilot.repositories.durable_runs import "
        "DurableRunRepository, WorkLeaseConflictError\n"
        "repo = DurableRunRepository(sys.argv[1])\n"
        "try:\n"
        "    repo.release_lease(work_id='work_247', lease_owner='still_working')\n"
        "except WorkLeaseConflictError:\n"
        "    print('release_rejected')\n"
        "else:\n"
        "    raise AssertionError('live Run lease was improperly deleted')\n"
    )
    process = subprocess.run(  # noqa: S603 - explicit local Python fixture
        [sys.executable, "-c", script, str(db)],
        cwd=_ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert process.returncode == 0, process.stderr
    assert "release_rejected" in process.stdout
    assert repo.inspect_lease("work_247")["lease_owner"] == "still_working"
    assert repo.get_run(run_id)["status"] == "RUNNING"
    assert repo.get_step(step_id)["status"] == "RUNNING"
    assert repo.list_step_attempts(step_id)[0]["status"] == "RUNNING"
