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
import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@contextmanager
def project_run_directory_lock(project_root: Path) -> Iterator[None]:
    """Cross-process writer/cleanup fence shared by legacy Run publishers.

    Store the lock *outside* runs/, so cleanup never unlinks its own lock.
    An OS advisory lock is automatically released after process termination.
    External processes that ignore this protocol cannot be serialized; the
    executor also checks directory identity and atomically detaches a
    candidate before recursive deletion.
    """
    project_root = Path(project_root)
    lock_path = project_root / ".run-directory.lock"
    if lock_path.is_symlink():
        raise OSError("Run directory lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _directory_identity(path: Path) -> tuple[int, int] | None:
    """Read an exact directory identity without following link replacements."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _current_latest(project_root: Path) -> str | None:
    """Unreadable latest pointers conservatively block cleanup."""
    try:
        return (project_root / "latest_run_id.txt").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError):
        return None


def _trusted_runs_parent(project_root: Path) -> tuple[int, int] | None:
    """Only a real Project/runs directory may authorize recursive deletion.

    lstat never follows symlinks; on Windows a junction is a reparse point,
    not a trusted real directory either. This is checked both when planning
    and while executing under the shared publisher/cleanup lock.
    """
    project_root = Path(project_root)
    runs_dir = project_root / "runs"
    try:
        project_info = project_root.lstat()
        runs_info = runs_dir.lstat()
        for info in (project_info, runs_info):
            if not stat.S_ISDIR(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                return None
        # A POSIX directory symlink has S_IFLNK (not S_IFDIR).
        # Reject linked/junction parents even when the target stays in Project.
        if project_root.is_symlink() or runs_dir.is_symlink():
            return None
        if runs_dir.resolve(strict=True) != project_root.resolve(strict=True) / "runs":
            return None
        return runs_info.st_dev, runs_info.st_ino
    except (OSError, RuntimeError):
        return None


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
    directory_device: int | None = None
    directory_inode: int | None = None
    runs_directory_device: int | None = None
    runs_directory_inode: int | None = None


@dataclass(frozen=True)
class RunCleanupPlan:
    """A computed plan of runs to delete, built from a policy."""

    project_id: str
    dry_run: bool
    protected_run_ids: list[str]
    candidates: list[RunCleanupCandidate]
    policy: RunCleanupPolicy | None = None


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



def _verified_run_start(value: object) -> datetime | None:
    """Parse a real, timezone-aware Run start, or fail closed.

    The legacy Run ID contains only seconds followed by eight random hex
    digits. That suffix is *not* a chronology tie-break for distinct starts.
    Missing, naive, malformed and non-string metadata can never authorize
    destructive keep-last cleanup; the affected directory is protected.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
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
    runs_identity = _trusted_runs_parent(project_root)
    if runs_identity is None:
        return RunCleanupPlan(
            project_id="",
            dry_run=policy.dry_run,
            protected_run_ids=[],
            candidates=[],
            policy=policy,
        )

    # Read latest_run_id.txt
    latest_file = project_root / "latest_run_id.txt"
    latest_run_id = ""
    if latest_file.exists():
        try:
            latest_run_id = latest_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    # Only a validated persisted start time may determine recency. The
    # second-resolution run ID's random suffix must never rank distinct Runs.
    # Legacy/invalid timestamps are conservatively protected, not deleted.
    run_entries: list[tuple[str, str, Path, datetime]] = []
    protected: list[str] = []
    candidates: list[RunCleanupCandidate] = []
    for child in sorted(runs_dir.iterdir()):
        if _directory_identity(child) is None:
            continue
        data = _read_run_json(child)
        if not isinstance(data, dict):
            continue
        run_id = data.get("run_id", child.name)
        status = data.get("status", "UNKNOWN")
        # Never plan a directory with mismatched/invalid persisted identity.
        if run_id != child.name or not isinstance(status, str):
            continue
        started = _verified_run_start(data.get("started_at"))
        if started is None:
            protected.append(run_id)
            continue
        run_entries.append((run_id, status, child, started))

    # A stable ID tie-break is used only for genuinely identical persisted
    # instants; arbitrary suffixes never override differing full timestamps.
    run_entries.sort(key=lambda e: (e[3], e[0]), reverse=True)

    for idx, (run_id, status, run_path, _started) in enumerate(run_entries):
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
            identity = _directory_identity(run_path)
            if identity is None:
                continue
            candidates.append(RunCleanupCandidate(
                run_id=run_id,
                status=status,
                path=str(run_path),
                reason=reason,
                directory_device=identity[0],
                directory_inode=identity[1],
                runs_directory_device=runs_identity[0],
                runs_directory_inode=runs_identity[1],
            ))
        else:
            protected.append(run_id)

    return RunCleanupPlan(
        project_id="",
        dry_run=policy.dry_run,
        protected_run_ids=protected,
        candidates=candidates,
        policy=policy,
    )


# ----------------------------------------------------------- execution
def execute_run_cleanup_plan(plan: RunCleanupPlan) -> RunCleanupResult:
    """Delete only candidates still eligible under the live Run directory lock.

    Run publishers use the same OS-backed cross-process lock to serialize
    status/latest writes and output mirroring with cleanup. Recompute policy
    eligibility, verify the *original* directory identity, then atomically
    detach the exact candidate into an unguessable quarantine pathname before
    recursive deletion. Never delete a replacement at a reused run path.
    """
    deleted: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    if plan.dry_run:
        return RunCleanupResult(
            project_id=plan.project_id, dry_run=True, deleted_run_ids=[],
            skipped_run_ids=[c.run_id for c in plan.candidates], errors=[],
        )

    for candidate in plan.candidates:
        path = Path(candidate.path)
        # Never traverse outside a conventional Project/runs/{run_id} folder.
        if path.name != candidate.run_id or path.parent.name != "runs":
            errors.append(f"{candidate.run_id}: invalid Run cleanup path")
            continue
        project_root = path.parent.parent
        try:
            with project_run_directory_lock(project_root):
                # The Run candidate and its PARENT must still be the
                # directories that were approved in the original plan.
                # In particular, reject a linked or swapped Project/runs.
                parent_identity = _trusted_runs_parent(project_root)
                planned_parent = (
                    candidate.runs_directory_device,
                    candidate.runs_directory_inode,
                )
                if parent_identity is None or (
                    None not in planned_parent and parent_identity != planned_parent
                ):
                    skipped.append(candidate.run_id)
                    continue
                identity = _directory_identity(path)
                original = (
                    candidate.directory_device, candidate.directory_inode
                )
                if (
                    identity is None
                    or (None not in original and identity != original)
                ):
                    skipped.append(candidate.run_id)
                    continue
                data = _read_run_json(path)
                if (
                    not isinstance(data, dict)
                    or data.get("run_id", path.name) != candidate.run_id
                    or data.get("status") != candidate.status
                ):
                    skipped.append(candidate.run_id)
                    continue
                policy = plan.policy
                current_latest = _current_latest(project_root)
                if (
                    current_latest is None
                    or (
                        (policy is None or policy.keep_latest)
                        and current_latest == candidate.run_id
                    )
                    or (
                        candidate.status == "RUNNING"
                        and (policy is None or not policy.delete_running)
                    )
                ):
                    skipped.append(candidate.run_id)
                    continue
                if policy is not None:
                    # New runs can change keep_last, not just latest/status.
                    fresh = build_run_cleanup_plan(project_root, policy)
                    if not any(
                        c.run_id == candidate.run_id and c.path == candidate.path
                        and c.status == candidate.status
                        for c in fresh.candidates
                    ):
                        skipped.append(candidate.run_id)
                        continue

                # Detach into the real Project root, NOT under runs/.
                # A hostile runs/ symlink swap must never redirect rmtree's
                # quarantine pathname outside this Project. The detached
                # object's inode and the runs parent are checked again below.
                parked = project_root / f".cleanup-quarantine-{secrets.token_hex(16)}"
                if parked.exists() or parked.is_symlink():
                    errors.append(f"{candidate.run_id}: quarantine path occupied")
                    continue
                path.rename(parked)
                # Recheck after atomic detach. An uncooperative process may
                # replace the original path, but cannot redirect our rmtree
                # to the new Run. Cooperating publishers hold the same lock.
                parked_identity = _directory_identity(parked)
                current_parent = _trusted_runs_parent(project_root)
                parked_data = _read_run_json(parked)
                new_latest = _current_latest(project_root)
                safe = (
                    parked_identity == identity
                    and current_parent == parent_identity
                    and isinstance(parked_data, dict)
                    and parked_data.get("run_id", candidate.run_id) == candidate.run_id
                    and parked_data.get("status") == candidate.status
                    and new_latest is not None
                    and not (
                        (policy is None or policy.keep_latest)
                        and new_latest == candidate.run_id
                    )
                )
                if not safe:
                    if (
                        current_parent == parent_identity
                        and not path.exists() and parked.exists()
                    ):
                        parked.rename(path)
                    else:
                        # Never restore through a changed/linked runs parent:
                        # it may now point outside Project. Retain the exact
                        # detached bytes in Project quarantine for recovery.
                        errors.append(
                            f"{candidate.run_id}: detached Run could not be restored"
                        )
                    skipped.append(candidate.run_id)
                    continue
                # Deletion only targets the detached private directory, not
                # the original name that another process could now create.
                # Keep the final recursive operation on the now-detached
                # real Project-root pathname, never through Project/runs.
                if _trusted_runs_parent(project_root) != parent_identity:
                    skipped.append(candidate.run_id)
                    errors.append(f"{candidate.run_id}: runs directory changed before deletion")
                    continue
                shutil.rmtree(parked)
                deleted.append(candidate.run_id)
                log.info("deleted retired run directory: %s", path)
        except OSError as exc:
            errors.append(f"{candidate.run_id}: {exc}")
            log.warning("failed to clean Run %s: %s", candidate.run_id, exc)

    return RunCleanupResult(
        project_id=plan.project_id, dry_run=False,
        deleted_run_ids=deleted, skipped_run_ids=skipped, errors=errors,
    )


__all__ = [
    "RunCleanupCandidate",
    "RunCleanupPlan",
    "RunCleanupPolicy",
    "RunCleanupResult",
    "build_run_cleanup_plan",
    "execute_run_cleanup_plan",
]
