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
import os
import secrets
import stat
from contextlib import contextmanager
from typing import Iterator
from dataclasses import dataclass
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
    try:
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
    except BaseException:
        raise


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

    # Collect all run directories with their metadata
    run_entries: list[tuple[str, str, Path]] = []  # (run_id, status, path)
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
                directory_device=_directory_identity(run_path)[0],
                directory_inode=_directory_identity(run_path)[1],
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
                if (
                    _current_latest(project_root) is None
                    or (
                        (policy is None or policy.keep_latest)
                        and _current_latest(project_root) == candidate.run_id
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

                parked = path.parent / f".cleanup-quarantine-{secrets.token_hex(16)}"
                if parked.exists() or parked.is_symlink():
                    errors.append(f"{candidate.run_id}: quarantine path occupied")
                    continue
                path.rename(parked)
                # Recheck after atomic detach. An uncooperative process may
                # replace the original path, but cannot redirect our rmtree
                # to the new Run. Cooperating publishers hold the same lock.
                parked_identity = _directory_identity(parked)
                parked_data = _read_run_json(parked)
                new_latest = _current_latest(project_root)
                safe = (
                    parked_identity == identity
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
                    if not path.exists() and parked.exists():
                        parked.rename(path)
                    else:
                        errors.append(
                            f"{candidate.run_id}: detached Run could not be restored"
                        )
                    skipped.append(candidate.run_id)
                    continue
                # Deletion only targets the detached private directory, not
                # the original name that another process could now create.
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
