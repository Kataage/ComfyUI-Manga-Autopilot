"""Run cleanup foundation for old per-run artifacts (issue #196).

Provides an explicit, safe cleanup service for old per-run artifacts.
Automatic background deletion is NOT implemented; this module only
provides plan building and explicit execution via the API.

Protects:
- The latest run (from ``latest_run_id.txt``)
- Running runs (when ``delete_running=False``)
- The most recent ``keep_last`` completed/failed/cancelled runs
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# ----------------------------------------------------------- policy model
@dataclass(frozen=True)
class RunCleanupPolicy:
    """Configuration for which old runs to delete.

    All deletion flags default to ``True`` (eligible for deletion) except
    ``delete_running`` which defaults to ``False`` (protected).
    """

    keep_latest: bool = True
    keep_last: int = 5
    delete_completed: bool = True
    delete_failed: bool = True
    delete_cancelled: bool = True
    delete_running: bool = False
    dry_run: bool = True


# ----------------------------------------------------------- plan model
@dataclass(frozen=True)
class RunCleanupCandidate:
    """A single run identified for potential deletion."""

    run_id: str
    status: str
    path: str
    reason: str


@dataclass(frozen=True)
class RunCleanupPlan:
    """A computed plan of runs to delete, built from a policy."""

    project_id: str
    dry_run: bool
    protected_run_ids: list[str]
    candidates: list[RunCleanupCandidate]


# ----------------------------------------------------------- result model
@dataclass(frozen=True)
class RunCleanupResult:
    """Outcome of executing a cleanup plan."""

    project_id: str
    dry_run: bool
    deleted_run_ids: list[str]
    skipped_run_ids: list[str]
    errors: list[str]


# ----------------------------------------------------------- plan builder
def _read_run_json(run_dir: Path) -> dict | None:
    """Read and parse ``run.json`` from a run directory.

    Returns ``None`` if the file is missing or corrupted.
    """
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return None
    try:
        return json.loads(run_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_run_cleanup_plan(
    project_root: Path,
    policy: RunCleanupPolicy,
) -> RunCleanupPlan:
    """Build a cleanup plan by scanning ``runs/`` under ``project_root``.

    The plan identifies which runs are protected and which are candidates
    for deletion based on the given :class:`RunCleanupPolicy`.
    """
    project_root = Path(project_root)
    runs_dir = project_root / "runs"
    if not runs_dir.is_dir():
        return RunCleanupPlan(
            project_id="",
            dry_run=policy.dry_run,
            protected_run_ids=[],
            candidates=[],
        )

    # Read latest_run_id.txt
    latest_file = project_root / "latest_run_id.txt"
    latest_run_id = ""
    if latest_file.exists():
        try:
            latest_run_id = latest_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    # Collect all run directories with their metadata
    run_entries: list[tuple[str, str, Path]] = []  # (run_id, status, path)
    for child in sorted(runs_dir.iterdir()):
        if not child.is_dir():
            continue
        data = _read_run_json(child)
        if data is None:
            continue
        run_id = data.get("run_id", child.name)
        status = data.get("status", "UNKNOWN")
        run_entries.append((run_id, status, child))

    # Sort by run_id descending (most recent first, based on timestamp)
    run_entries.sort(key=lambda e: e[0], reverse=True)

    protected: list[str] = []
    candidates: list[RunCleanupCandidate] = []

    for idx, (run_id, status, run_path) in enumerate(run_entries):
        # Protect latest run
        if policy.keep_latest and run_id == latest_run_id:
            protected.append(run_id)
            continue

        # Protect running runs
        if not policy.delete_running and status == "RUNNING":
            protected.append(run_id)
            continue

        # Protect keep_last most recent runs (after latest)
        if idx < policy.keep_last:
            protected.append(run_id)
            continue

        # Check if this status is eligible for deletion
        eligible = False
        reason = ""
        if status == "COMPLETED" and policy.delete_completed:
            eligible = True
            reason = "older than keep_last"
        elif status.startswith("FAILED") and policy.delete_failed:
            eligible = True
            reason = "older than keep_last"
        elif status == "CANCELLED" and policy.delete_cancelled:
            eligible = True
            reason = "older than keep_last"

        if eligible:
            candidates.append(RunCleanupCandidate(
                run_id=run_id,
                status=status,
                path=str(run_path),
                reason=reason,
            ))
        else:
            protected.append(run_id)

    return RunCleanupPlan(
        project_id="",
        dry_run=policy.dry_run,
        protected_run_ids=protected,
        candidates=candidates,
    )


# ----------------------------------------------------------- execution
def execute_run_cleanup_plan(plan: RunCleanupPlan) -> RunCleanupResult:
    """Execute a cleanup plan, deleting candidate run directories.

    If ``plan.dry_run`` is ``True``, no directories are actually deleted.
    """
    deleted: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    if plan.dry_run:
        return RunCleanupResult(
            project_id=plan.project_id,
            dry_run=True,
            deleted_run_ids=[],
            skipped_run_ids=[c.run_id for c in plan.candidates],
            errors=[],
        )

    for candidate in plan.candidates:
        run_path = Path(candidate.path)
        if not run_path.is_dir():
            skipped.append(candidate.run_id)
            continue
        try:
            shutil.rmtree(run_path)
            deleted.append(candidate.run_id)
            log.info("deleted run directory: %s", run_path)
        except OSError as exc:
            errors.append(f"{candidate.run_id}: {exc}")
            log.warning("failed to delete %s: %s", run_path, exc)

    return RunCleanupResult(
        project_id=plan.project_id,
        dry_run=False,
        deleted_run_ids=deleted,
        skipped_run_ids=skipped,
        errors=errors,
    )


__all__ = [
    "RunCleanupCandidate",
    "RunCleanupPlan",
    "RunCleanupPolicy",
    "RunCleanupResult",
    "build_run_cleanup_plan",
    "execute_run_cleanup_plan",
]
