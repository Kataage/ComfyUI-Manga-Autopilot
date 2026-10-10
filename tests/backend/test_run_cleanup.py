"""Tests for run cleanup foundation (issue #196).

Covers:
- Cleanup policy model
- Cleanup plan building (latest protection, keep_last, status flags, running protection)
- Cleanup execution (dry-run, actual deletion)
- Cleanup API endpoint
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web

from manga_autopilot.routes import register_all
from manga_autopilot.services.run_cleanup import (
    RunCleanupPolicy,
    build_run_cleanup_plan,
    execute_run_cleanup_plan,
)


# --------------------------------------------------------------- helpers
def _create_fake_run(
    project_root: Path,
    run_id: str,
    status: str = "COMPLETED",
) -> Path:
    """Create a fake run directory with run.json."""
    run_dir = project_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_data = {
        "run_id": run_id,
        "project_id": "proj_test",
        "status": status,
        "started_at": "2026-01-01T00:00:00Z",
        "input": {},
    }
    (run_dir / "run.json").write_text(json.dumps(run_data), encoding="utf-8")
    return run_dir


# --------------------------------------------------------------- plan tests
def test_cleanup_plan_keeps_latest_run(tmp_path: Path) -> None:
    """Latest run is always protected."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "runs").mkdir()

    _create_fake_run(project_root, "run_001", "COMPLETED")
    _create_fake_run(project_root, "run_002", "COMPLETED")
    (project_root / "latest_run_id.txt").write_text("run_002")

    policy = RunCleanupPolicy(keep_last=0)
    plan = build_run_cleanup_plan(project_root, policy)

    assert "run_002" in plan.protected_run_ids
    assert all(c.run_id != "run_002" for c in plan.candidates)


def test_cleanup_plan_keeps_last_n_runs(tmp_path: Path) -> None:
    """keep_last protects the N most recent runs."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "runs").mkdir()

    for i in range(5):
        _create_fake_run(project_root, f"run_{i:03d}", "COMPLETED")
    (project_root / "latest_run_id.txt").write_text("run_004")

    policy = RunCleanupPolicy(keep_last=3)
    plan = build_run_cleanup_plan(project_root, policy)

    # keep_last=3 means 3 most recent are protected + latest
    protected_ids = set(plan.protected_run_ids)
    assert "run_004" in protected_ids  # latest
    assert "run_003" in protected_ids  # keep_last
    assert "run_002" in protected_ids  # keep_last
    candidate_ids = {c.run_id for c in plan.candidates}
    assert "run_001" in candidate_ids
    assert "run_000" in candidate_ids


def test_cleanup_plan_excludes_running_runs_by_default(tmp_path: Path) -> None:
    """Running runs are protected by default."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "runs").mkdir()

    _create_fake_run(project_root, "run_001", "RUNNING")
    _create_fake_run(project_root, "run_002", "COMPLETED")
    (project_root / "latest_run_id.txt").write_text("run_002")

    policy = RunCleanupPolicy(keep_last=0)
    plan = build_run_cleanup_plan(project_root, policy)

    assert "run_001" in plan.protected_run_ids
    assert all(c.run_id != "run_001" for c in plan.candidates)


def test_cleanup_plan_respects_status_flags(tmp_path: Path) -> None:
    """Status flags control which runs are eligible for deletion."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "runs").mkdir()

    _create_fake_run(project_root, "run_001", "COMPLETED")
    _create_fake_run(project_root, "run_002", "FAILED_EXPORT")
    _create_fake_run(project_root, "run_003", "CANCELLED")
    (project_root / "latest_run_id.txt").write_text("run_003")

    policy = RunCleanupPolicy(
        keep_last=0,
        delete_completed=False,
        delete_failed=True,
        delete_cancelled=True,
    )
    plan = build_run_cleanup_plan(project_root, policy)

    candidate_ids = {c.run_id for c in plan.candidates}
    assert "run_001" not in candidate_ids  # completed not deleted
    assert "run_002" in candidate_ids  # failed deleted
    assert "run_003" not in candidate_ids  # latest protected


# --------------------------------------------------------------- execution tests
def test_cleanup_dry_run_does_not_delete_directories(tmp_path: Path) -> None:
    """Dry-run does not delete any directories."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "runs").mkdir()

    _create_fake_run(project_root, "run_001", "COMPLETED")
    _create_fake_run(project_root, "run_002", "COMPLETED")
    (project_root / "latest_run_id.txt").write_text("run_002")

    policy = RunCleanupPolicy(keep_last=0, dry_run=True)
    plan = build_run_cleanup_plan(project_root, policy)
    result = execute_run_cleanup_plan(plan)

    assert result.dry_run is True
    assert result.deleted_run_ids == []
    assert (project_root / "runs" / "run_001").exists()
    assert (project_root / "runs" / "run_002").exists()


def test_cleanup_execute_deletes_candidates(tmp_path: Path) -> None:
    """Execute deletes candidate run directories when dry_run=False."""
    from manga_autopilot.services.run_cleanup import RunCleanupPlan

    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "runs").mkdir()

    _create_fake_run(project_root, "run_001", "COMPLETED")
    _create_fake_run(project_root, "run_002", "COMPLETED")
    (project_root / "latest_run_id.txt").write_text("run_002")

    plan = RunCleanupPlan(
        project_id="proj_test",
        dry_run=False,
        protected_run_ids=["run_002"],
        candidates=[
            {
                "run_id": "run_001",
                "status": "COMPLETED",
                "path": str(project_root / "runs" / "run_001"),
                "reason": "older than keep_last",
            },
        ],
    )
    # Convert dict candidates to RunCleanupCandidate objects
    from manga_autopilot.services.run_cleanup import RunCleanupCandidate
    plan = RunCleanupPlan(
        project_id=plan.project_id,
        dry_run=plan.dry_run,
        protected_run_ids=plan.protected_run_ids,
        candidates=[
            RunCleanupCandidate(**c) if isinstance(c, dict) else c
            for c in plan.candidates
        ],
    )

    result = execute_run_cleanup_plan(plan)

    assert result.dry_run is False
    assert "run_001" in result.deleted_run_ids
    assert not (project_root / "runs" / "run_001").exists()
    assert (project_root / "runs" / "run_002").exists()


# --------------------------------------------------------------- API tests
@pytest.mark.asyncio()
async def test_cleanup_api_returns_dry_run_plan(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Cleanup API returns dry-run plan by default."""
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    client = await aiohttp_client(app)

    # Create project
    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Cleanup Test"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Create fake runs
    project_root = tmp_path / "projects" / project_id
    _create_fake_run(project_root, "run_001", "COMPLETED")
    _create_fake_run(project_root, "run_002", "COMPLETED")
    (project_root / "latest_run_id.txt").write_text("run_002")

    # Call cleanup API (default dry_run=true)
    cleanup_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/runs/cleanup",
        json={"keep_last": 0},
    )
    assert cleanup_resp.status == 200
    data = await cleanup_resp.json()

    assert data["dry_run"] is True
    assert "run_002" in data["protected_run_ids"]
    assert len(data["deleted_run_ids"]) == 0
    assert any(c["run_id"] == "run_001" for c in data["candidates"])

    # Verify run_001 still exists
    assert (project_root / "runs" / "run_001").exists()


@pytest.mark.asyncio()
async def test_cleanup_api_deletes_old_runs_when_dry_run_false(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Cleanup API deletes runs when dry_run=false."""
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    client = await aiohttp_client(app)

    # Create project
    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Cleanup Delete"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Create fake runs
    project_root = tmp_path / "projects" / project_id
    _create_fake_run(project_root, "run_001", "COMPLETED")
    _create_fake_run(project_root, "run_002", "COMPLETED")
    (project_root / "latest_run_id.txt").write_text("run_002")

    # Call cleanup API with dry_run=false
    cleanup_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/runs/cleanup",
        json={"keep_last": 0, "dry_run": False},
    )
    assert cleanup_resp.status == 200
    data = await cleanup_resp.json()

    assert data["dry_run"] is False
    assert "run_001" in data["deleted_run_ids"]
    assert not (project_root / "runs" / "run_001").exists()
    assert (project_root / "runs" / "run_002").exists()


@pytest.mark.asyncio()
async def test_cleanup_api_never_deletes_latest_run(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Cleanup API never deletes the latest run, even with keep_last=0."""
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    client = await aiohttp_client(app)

    # Create project
    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Cleanup Latest"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Create fake runs
    project_root = tmp_path / "projects" / project_id
    _create_fake_run(project_root, "run_001", "COMPLETED")
    (project_root / "latest_run_id.txt").write_text("run_001")

    # Call cleanup API with keep_last=0 and dry_run=false
    cleanup_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/runs/cleanup",
        json={"keep_last": 0, "dry_run": False},
    )
    assert cleanup_resp.status == 200
    data = await cleanup_resp.json()

    assert "run_001" in data["protected_run_ids"]
    assert "run_001" not in data["deleted_run_ids"]
    assert (project_root / "runs" / "run_001").exists()


@pytest.mark.asyncio()
async def test_cleanup_api_returns_404_for_missing_project(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Cleanup API returns 404 for non-existent project."""
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    client = await aiohttp_client(app)

    cleanup_resp = await client.post(
        "/manga_autopilot/api/projects/nonexistent/runs/cleanup",
        json={},
    )
    assert cleanup_resp.status == 404

@pytest.mark.parametrize(
    "became_protected",
    ["running_after_plan", "latest_after_plan", "replaced_by_running_run"],
)
def test_phase_c_independent_audit_cleanup_revalidates_protection_before_deletion(
    tmp_path: Path, became_protected: str,
) -> None:
    """A cleanup plan cannot delete a run newly protected before execution.

    This test-only Phase C audit uses public cleanup service entrypoints.
    Deliberately freeze the candidate plan, change authoritative run status,
    latest-run reference, or directory identity, then execute. The default
    policy protects RUNNING and latest runs even if plan data is stale.
    Expected RED on merged post-#413 develop without any production change.
    """
    project = tmp_path / "proj"
    project.mkdir()
    older = _create_fake_run(project, "run_001", "COMPLETED")
    _create_fake_run(project, "run_002", "COMPLETED")
    latest = project / "latest_run_id.txt"
    latest.write_text("run_002", encoding="utf-8")

    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    assert [c.run_id for c in plan.candidates] == ["run_001"]
    assert older.is_dir()

    if became_protected == "running_after_plan":
        data = json.loads((older / "run.json").read_text(encoding="utf-8"))
        data["status"] = "RUNNING"
        (older / "run.json").write_text(json.dumps(data), encoding="utf-8")
    elif became_protected == "latest_after_plan":
        latest.write_text("run_001", encoding="utf-8")
    else:
        # A replacement under the same pathname is not the Work/Run that
        # was originally inspected. Do not delete this newly active Run.
        retired = project / "runs" / "retired_run_001"
        older.rename(retired)
        newer = _create_fake_run(project, "run_001", "RUNNING")
        assert newer == older

    result = execute_run_cleanup_plan(plan)
    assert older.is_dir(), (
        "cleanup deleted the current RUNNING/latest/replacement Run "
        f"after its snapshot became stale: {became_protected}"
    )
    assert "run_001" not in result.deleted_run_ids
    assert "run_001" in result.skipped_run_ids

def test_issue415_run_cleanup_writer_lock_and_stale_directory(
    tmp_path: Path, monkeypatch,
) -> None:
    """Execution waits for the publisher lock and then rereads current status."""
    import threading
    from contextlib import contextmanager

    import manga_autopilot.services.run_cleanup as cleanup

    project = tmp_path / "project"
    project.mkdir()
    target = _create_fake_run(project, "run_001", "COMPLETED")
    _create_fake_run(project, "run_002", "COMPLETED")
    (project / "latest_run_id.txt").write_text("run_002", encoding="utf-8")
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    original_lock = cleanup.project_run_directory_lock
    waiting = threading.Event()
    acquired = threading.Event()
    outcomes = []

    @contextmanager
    def traced_lock(root):
        waiting.set()
        with original_lock(root):
            acquired.set()
            yield

    monkeypatch.setattr(cleanup, "project_run_directory_lock", traced_lock)

    def cleanup_worker():
        outcomes.append(execute_run_cleanup_plan(plan))

    with original_lock(project):
        worker = threading.Thread(target=cleanup_worker, daemon=True)
        worker.start()
        assert waiting.wait(timeout=5)
        assert not acquired.is_set(), "cleanup bypassed active publisher lock"
        data = json.loads((target / "run.json").read_text(encoding="utf-8"))
        data["status"] = "RUNNING"
        (target / "run.json").write_text(json.dumps(data), encoding="utf-8")
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert acquired.is_set()
    assert target.is_dir()
    assert outcomes[0].deleted_run_ids == []
    assert outcomes[0].skipped_run_ids == ["run_001"]


def test_issue415_cleanup_detects_directory_swap_during_atomic_detach(
    tmp_path: Path, monkeypatch,
) -> None:
    """A replacement introduced at rename time cannot be deleted."""
    project = tmp_path / "project"
    project.mkdir()
    target = _create_fake_run(project, "run_001", "COMPLETED")
    _create_fake_run(project, "run_002", "COMPLETED")
    (project / "latest_run_id.txt").write_text("run_002", encoding="utf-8")
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    original_rename = Path.rename
    swapped = []

    def replace_target(self, destination):
        if self == target and not swapped:
            moved = self.parent / "old_run_001"
            original_rename(self, moved)
            _create_fake_run(project, "run_001", "RUNNING")
            swapped.append(moved)
        return original_rename(self, destination)

    monkeypatch.setattr(Path, "rename", replace_target)
    result = execute_run_cleanup_plan(plan)
    assert len(swapped) == 1
    assert target.is_dir()
    assert json.loads((target / "run.json").read_text(encoding="utf-8"))["status"] == "RUNNING"
    assert swapped[0].is_dir()
    assert result.deleted_run_ids == []
    assert result.skipped_run_ids == ["run_001"]


def test_issue415_keep_last_is_rechecked_against_current_run_order(
    tmp_path: Path,
) -> None:
    """The retention ranking may change between plan and execute."""
    project = tmp_path / "project"
    project.mkdir()
    oldest = _create_fake_run(project, "run_001", "COMPLETED")
    _create_fake_run(project, "run_002", "COMPLETED")
    newest = _create_fake_run(project, "run_003", "COMPLETED")
    (project / "latest_run_id.txt").write_text("run_003", encoding="utf-8")
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=2, dry_run=False),
    )
    assert [c.run_id for c in plan.candidates] == ["run_001"]
    import shutil

    shutil.rmtree(newest)
    result = execute_run_cleanup_plan(plan)
    assert oldest.is_dir()
    assert result.deleted_run_ids == []
    assert result.skipped_run_ids == ["run_001"]
