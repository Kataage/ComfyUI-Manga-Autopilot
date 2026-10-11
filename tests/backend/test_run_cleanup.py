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

def _phase_c_audit_same_second_retention_runs(
    project: Path,
) -> tuple[str, str, str]:
    """Use the real run_YYYYMMDD_HHMMSS_<random> ID contract.

    A later-in-time Run gets a smaller random suffix; sorting opaque Run IDs
    as if they were timestamps reverses the true order within the same second.
    """
    older_id = "run_20261011_100000_ffffffff"
    newer_id = "run_20261011_100000_00000000"
    latest_id = "run_20261011_100001_11111111"
    for run_id, started_at in (
        (older_id, "2026-10-11T10:00:00.100000+00:00"),
        (newer_id, "2026-10-11T10:00:00.900000+00:00"),
        (latest_id, "2026-10-11T10:00:01.100000+00:00"),
    ):
        folder = _create_fake_run(project, run_id, "COMPLETED")
        meta = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        meta["started_at"] = started_at
        (folder / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    (project / "latest_run_id.txt").write_text(latest_id, encoding="utf-8")
    return older_id, newer_id, latest_id


def test_phase_c_independent_audit_keep_last_uses_actual_run_start_not_random_suffix(
    tmp_path: Path,
) -> None:
    """Protect the two most recent Runs even with inverted random suffixes."""
    project = tmp_path / "project"
    project.mkdir()
    older, newer, latest = _phase_c_audit_same_second_retention_runs(project)
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=2, dry_run=False),
    )
    assert latest in plan.protected_run_ids
    assert newer in plan.protected_run_ids
    assert older not in plan.protected_run_ids
    assert [item.run_id for item in plan.candidates] == [older]


def test_phase_c_independent_audit_cleanup_never_deletes_newer_same_second_run(
    tmp_path: Path,
) -> None:
    """An actual destructive cleanup must retain newer Run bytes and history."""
    project = tmp_path / "project"
    project.mkdir()
    older, newer, latest = _phase_c_audit_same_second_retention_runs(project)
    newer_dir = project / "runs" / newer
    older_dir = project / "runs" / older
    (newer_dir / "precious-newer-output.bin").write_bytes(b"newer output")
    (older_dir / "retired-older-output.bin").write_bytes(b"retired output")

    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=2, dry_run=False),
    )
    result = execute_run_cleanup_plan(plan)
    assert result.errors == []
    assert result.deleted_run_ids == [older]
    assert older_dir.exists() is False
    assert newer_dir.is_dir()
    assert (newer_dir / "precious-newer-output.bin").read_bytes() == b"newer output"
    assert (project / "runs" / latest).is_dir()

@pytest.mark.parametrize(
    "invalid_start",
    [None, "", "not-a-time", "2026-10-11T10:00:00", 123],
)
def test_issue421_invalid_run_start_is_protected_from_cleanup(
    tmp_path: Path, invalid_start: object,
) -> None:
    """Missing, naive and malformed metadata cannot authorize deletion."""
    project = tmp_path / "project"
    project.mkdir()
    target = _create_fake_run(project, "run_001", "COMPLETED")
    latest = _create_fake_run(project, "run_002", "COMPLETED")
    (project / "latest_run_id.txt").write_text("run_002", encoding="utf-8")
    data = json.loads((target / "run.json").read_text(encoding="utf-8"))
    if invalid_start is None:
        data.pop("started_at", None)
    else:
        data["started_at"] = invalid_start
    (target / "run.json").write_text(json.dumps(data), encoding="utf-8")

    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    assert "run_001" in plan.protected_run_ids
    assert all(candidate.run_id != "run_001" for candidate in plan.candidates)
    result = execute_run_cleanup_plan(plan)
    assert result.deleted_run_ids == []
    assert target.is_dir() and latest.is_dir()


def test_issue421_compare_started_at_in_utc_not_local_clock(
    tmp_path: Path,
) -> None:
    """An offset-qualified timestamp beats any randomized Run ID suffix."""
    project = tmp_path / "project"
    project.mkdir()
    older, newer, _latest = _phase_c_audit_same_second_retention_runs(project)
    newer_file = project / "runs" / newer / "run.json"
    data = json.loads(newer_file.read_text(encoding="utf-8"))
    # UTC 10:00:00.900 must be later than UTC 10:00:00.100.
    data["started_at"] = "2026-10-11T19:00:00.900000+09:00"
    newer_file.write_text(json.dumps(data), encoding="utf-8")

    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=2, dry_run=False),
    )
    assert [candidate.run_id for candidate in plan.candidates] == [older]
    assert newer in plan.protected_run_ids


def test_issue421_execution_rechecks_new_started_at_under_run_lock(
    tmp_path: Path,
) -> None:
    """A timestamp mutation after plan creation may revoke delete consent."""
    project = tmp_path / "project"
    project.mkdir()
    older, _newer, _latest = _phase_c_audit_same_second_retention_runs(project)
    target = project / "runs" / older
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=2, dry_run=False),
    )
    assert [candidate.run_id for candidate in plan.candidates] == [older]

    data = json.loads((target / "run.json").read_text(encoding="utf-8"))
    data["started_at"] = "2026-10-11T10:00:01.900000+00:00"
    (target / "run.json").write_text(json.dumps(data), encoding="utf-8")
    result = execute_run_cleanup_plan(plan)
    assert result.errors == []
    assert result.deleted_run_ids == []
    assert result.skipped_run_ids == [older]
    assert target.is_dir()



def _audit_linked_runs_root(tmp_path: Path) -> tuple[Path, Path]:
    """Keep the deletion target inside pytest's temporary area, but outside Project."""
    outside_project = tmp_path / "outside_project"
    outside_project.mkdir()
    outside_run = _create_fake_run(outside_project, "run_outside", "COMPLETED")
    (outside_run / "important.txt").write_text("must survive")
    project = tmp_path / "project"
    project.mkdir()
    try:
        (project / "runs").symlink_to(
            outside_project / "runs", target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks require platform privileges")
    return project, outside_run


def test_phase_c_post423_audit_cleanup_plan_rejects_symlinked_runs_root(
    tmp_path: Path,
) -> None:
    """A Project/runs link must never authorize external directory deletion."""
    project, outside_run = _audit_linked_runs_root(tmp_path)
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    assert plan.candidates == [], (
        "run cleanup must not enumerate external Runs through a linked parent"
    )
    assert outside_run.is_dir()


def test_phase_c_post423_audit_cleanup_execute_preserves_symlinked_runs_root(
    tmp_path: Path,
) -> None:
    """Even a live plan must never recursively delete an external Run."""
    project, outside_run = _audit_linked_runs_root(tmp_path)
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    result = execute_run_cleanup_plan(plan)
    assert outside_run.is_dir(), (
        "Run cleanup removed a directory outside the Project through runs/ link"
    )
    assert (outside_run / "important.txt").read_text() == "must survive"
    assert "run_outside" not in result.deleted_run_ids



def test_issue425_rejects_linked_runs_parent_added_after_valid_plan(
    tmp_path: Path,
) -> None:
    """A stale plan cannot delete a matching Run in a newly linked foreign tree."""
    project = tmp_path / "project"
    project.mkdir()
    _create_fake_run(project, "run_old", "COMPLETED")
    _create_fake_run(project, "run_latest", "COMPLETED")
    (project / "latest_run_id.txt").write_text("run_latest")
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    assert [c.run_id for c in plan.candidates] == ["run_old"]

    original_runs = project / "runs"
    original_runs.rename(project / "saved_runs")
    outside = tmp_path / "other_project"
    outside.mkdir()
    foreign = _create_fake_run(outside, "run_old", "COMPLETED")
    (foreign / "must_survive.txt").write_text("foreign")
    try:
        original_runs.symlink_to(outside / "runs", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks require platform privileges")

    result = execute_run_cleanup_plan(plan)
    assert result.deleted_run_ids == []
    assert result.skipped_run_ids == ["run_old"]
    assert (foreign / "must_survive.txt").read_text() == "foreign"
    assert (project / "saved_runs" / "run_old" / "run.json").exists()


def test_issue425_rejects_replaced_real_runs_parent_after_plan(
    tmp_path: Path,
) -> None:
    """A fresh real directory under the same lexical name is not the old plan."""
    project = tmp_path / "project"
    project.mkdir()
    original = _create_fake_run(project, "run_old", "COMPLETED")
    _create_fake_run(project, "run_latest", "COMPLETED")
    (project / "latest_run_id.txt").write_text("run_latest")
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    assert [c.run_id for c in plan.candidates] == ["run_old"]

    (project / "runs").rename(project / "saved_runs")
    replacement = _create_fake_run(project, "run_old", "COMPLETED")
    (replacement / "preserve.txt").write_text("new parent")
    result = execute_run_cleanup_plan(plan)
    assert result.deleted_run_ids == []
    assert result.skipped_run_ids == ["run_old"]
    assert (replacement / "preserve.txt").read_text() == "new parent"
    assert (project / "saved_runs" / original.name / "run.json").exists()


def test_issue425_legitimate_cleanup_uses_project_root_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal deletion stays inside the Project and frees its quarantine."""
    project = tmp_path / "project"
    project.mkdir()
    old = _create_fake_run(project, "run_old", "COMPLETED")
    _create_fake_run(project, "run_latest", "COMPLETED")
    (project / "latest_run_id.txt").write_text("run_latest")
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=0, dry_run=False),
    )
    original_rename = Path.rename
    moved_to = []

    def capture_rename(self: Path, destination: Path):
        if self == old:
            moved_to.append(Path(destination))
        return original_rename(self, destination)

    monkeypatch.setattr(Path, "rename", capture_rename)
    result = execute_run_cleanup_plan(plan)
    assert result.errors == []
    assert result.deleted_run_ids == ["run_old"]
    assert len(moved_to) == 1
    assert moved_to[0].parent == project
    assert moved_to[0].name.startswith(".cleanup-quarantine-")
    assert not moved_to[0].exists()
    assert not old.exists()
    assert (project / "runs" / "run_latest" / "run.json").exists()



def _phase_c_post425_active_run_retention_case(
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    """A newer still-RUNNING Run must not consume the terminal retention budget."""
    project = tmp_path / "retention_project"
    project.mkdir()
    old = _create_fake_run(project, "run_completed_old", "COMPLETED")
    keep = _create_fake_run(project, "run_completed_keep", "COMPLETED")
    active = _create_fake_run(project, "run_active_latest", "RUNNING")
    for directory, started in (
        (old, "2026-10-11T10:00:00+00:00"),
        (keep, "2026-10-11T11:00:00+00:00"),
        (active, "2026-10-11T12:00:00+00:00"),
    ):
        metadata = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        metadata["started_at"] = started
        (directory / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
    (project / "latest_run_id.txt").write_text("run_active_latest")
    (keep / "precious_completed.png").write_bytes(b"retained successful export")
    return project, old, keep


def test_phase_c_post425_audit_running_run_does_not_consume_keep_last(
    tmp_path: Path,
) -> None:
    """Retention counts the most recent terminal Run, not an active Run."""
    project, old, keep = _phase_c_post425_active_run_retention_case(tmp_path)
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=1, dry_run=False),
    )
    assert "run_active_latest" in plan.protected_run_ids
    assert "run_completed_keep" in plan.protected_run_ids
    assert [candidate.run_id for candidate in plan.candidates] == [
        "run_completed_old"
    ]
    assert old.is_dir() and keep.is_dir()


def test_phase_c_post425_audit_cleanup_preserves_recent_completed_run_with_active_latest(
    tmp_path: Path,
) -> None:
    """Destructive cleanup must preserve recent completed pages while Run runs."""
    project, old, keep = _phase_c_post425_active_run_retention_case(tmp_path)
    plan = build_run_cleanup_plan(
        project, RunCleanupPolicy(keep_last=1, dry_run=False),
    )
    result = execute_run_cleanup_plan(plan)
    assert result.deleted_run_ids == ["run_completed_old"]
    assert not old.exists()
    assert keep.is_dir(), "newest completed Run was deleted despite keep_last=1"
    assert (keep / "precious_completed.png").read_bytes() == (
        b"retained successful export"
    )
    assert (project / "runs" / "run_active_latest" / "run.json").is_file()
