"""Modal worker production hardening for Manga Autopilot.

Provides operational safety controls for Modal GPU workers:
request authentication, timeout policy, job TTL, stale job cleanup,
concurrency guard, and structured diagnostics.

CI does **not** run Modal.  All helpers are pure-Python.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# --------------------------------------------------------- error codes

ERROR_CODES = {
    "UNAUTHORIZED": "unauthorized",
    "WORKER_BUSY": "worker busy",
    "STARTUP_TIMEOUT": "startup timeout",
    "GENERATION_TIMEOUT": "generation timeout",
    "UPLOAD_TIMEOUT": "upload timeout",
    "CHECKPOINT_NOT_FOUND": "checkpoint not found",
    "WORKFLOW_INVALID": "workflow invalid",
    "COMFYUI_NOT_READY": "ComfyUI not ready",
    "ARTIFACT_UPLOAD_FAILED": "artifact upload failed",
    "JOB_NOT_FOUND": "job not found",
    "JOB_EXPIRED": "job expired",
    "JOB_CANCELLED": "job cancelled",
    "UNKNOWN_ERROR": "unknown error",
}


def build_structured_error(
    message: str,
    *,
    error_code: str = "UNKNOWN_ERROR",
    executor: str = "modal-comfyui",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured error response with error_code."""
    meta: dict[str, Any] = {"executor": executor}
    if metadata:
        meta.update(metadata)
    return {
        "status": "error",
        "error": message,
        "error_code": error_code,
        "metadata": meta,
    }


# ------------------------------------------------------- timeout policy


@dataclass(frozen=True)
class ModalWorkerTimeoutPolicy:
    """Timeout configuration for Modal worker operations.

    Parameters
    ----------
    startup_timeout_sec:
        Max seconds to wait for ComfyUI server startup.
    generation_timeout_sec:
        Max seconds for a single generation request.
    upload_timeout_sec:
        Max seconds for artifact upload.
    job_ttl_sec:
        Time-to-live for async jobs (expired jobs are cleaned up).
    """

    startup_timeout_sec: int = 120
    generation_timeout_sec: int = 600
    upload_timeout_sec: int = 120
    job_ttl_sec: int = 3600

    def __post_init__(self) -> None:
        for field_name in ("startup_timeout_sec", "generation_timeout_sec",
                           "upload_timeout_sec", "job_ttl_sec"):
            val = getattr(self, field_name)
            if val <= 0:
                raise ValueError(f"{field_name} must be > 0, got {val}")


def create_timeout_policy_from_env() -> ModalWorkerTimeoutPolicy:
    """Create a timeout policy from environment variables.

    Environment variables
    ---------------------
    - ``MANGA_AUTOPILOT_MODAL_STARTUP_TIMEOUT_SEC``: default 120
    - ``MANGA_AUTOPILOT_MODAL_GENERATION_TIMEOUT_SEC``: default 600
    - ``MANGA_AUTOPILOT_MODAL_UPLOAD_TIMEOUT_SEC``: default 120
    - ``MANGA_AUTOPILOT_MODAL_JOB_TTL_SEC``: default 3600
    """
    return ModalWorkerTimeoutPolicy(
        startup_timeout_sec=int(os.environ.get("MANGA_AUTOPILOT_MODAL_STARTUP_TIMEOUT_SEC", "120")),
        generation_timeout_sec=int(os.environ.get("MANGA_AUTOPILOT_MODAL_GENERATION_TIMEOUT_SEC", "600")),
        upload_timeout_sec=int(os.environ.get("MANGA_AUTOPILOT_MODAL_UPLOAD_TIMEOUT_SEC", "120")),
        job_ttl_sec=int(os.environ.get("MANGA_AUTOPILOT_MODAL_JOB_TTL_SEC", "3600")),
    )


# -------------------------------------------------------- auth helpers


def check_bearer_token(
    authorization: str | None,
    expected_token: str,
) -> bool:
    """Check if the Authorization header matches the expected bearer token.

    Returns ``True`` if valid, ``False`` otherwise.
    """
    if not expected_token:
        return True
    if not authorization:
        return False
    match = re.match(r"^Bearer\s+(.+)$", authorization, re.IGNORECASE)
    if not match:
        return False
    return match.group(1) == expected_token


def is_auth_required(
    *,
    expected_token: str,
    health_requires_auth: bool,
    path: str,
) -> bool:
    """Determine if auth is required for the given path.

    Parameters
    ----------
    expected_token:
        The expected bearer token (empty = no auth).
    health_requires_auth:
        Whether /v1/health requires auth.
    path:
        The request path.
    """
    if not expected_token:
        return False
    if path in ("/v1/health", "/v1/diagnostics") and not health_requires_auth:
        return False
    return True


def create_auth_config_from_env() -> tuple[str, bool]:
    """Create auth config from environment variables.

    Returns ``(token, health_requires_auth)``.
    """
    token = os.environ.get("MANGA_AUTOPILOT_MODAL_WORKER_TOKEN", "")
    health_auth = os.environ.get("MANGA_AUTOPILOT_MODAL_HEALTH_REQUIRES_AUTH", "false").lower() in ("true", "1", "yes")
    return token, health_auth


# ------------------------------------------------------- async job model


@dataclass
class AsyncJob:
    """Metadata for an async generation job.

    Parameters
    ----------
    job_id:
        Unique job identifier.
    status:
        Current job status.
    created_at:
        ISO-8601 UTC timestamp when the job was created.
    started_at:
        ISO-8601 UTC timestamp when execution started (``None`` if queued).
    completed_at:
        ISO-8601 UTC timestamp when the job completed.
    cancelled_at:
        ISO-8601 UTC timestamp when the job was cancelled.
    expires_at:
        ISO-8601 UTC timestamp when the job expires.
    error:
        Error message if status is ``"error"``.
    error_code:
        Error code if status is ``"error"``.
    metadata:
        Additional metadata (run_id, project_id, workflow_id).
    """

    job_id: str
    status: Literal["queued", "running", "completed", "error", "cancelled", "expired"] = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    expires_at: str | None = None
    error: str | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cancelled_at": self.cancelled_at,
            "expires_at": self.expires_at,
            "error": self.error,
            "error_code": self.error_code,
            "metadata": dict(self.metadata) if self.metadata else {},
        }


def create_async_job(
    job_id: str,
    *,
    job_ttl_sec: int = 3600,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AsyncJob:
    """Create a new async job with TTL."""
    created = now or datetime.now(timezone.utc)
    expires = created.timestamp() + job_ttl_sec
    expires_dt = datetime.fromtimestamp(expires, tz=timezone.utc)
    return AsyncJob(
        job_id=job_id,
        status="queued",
        created_at=created.isoformat(),
        expires_at=expires_dt.isoformat(),
        metadata=metadata or {},
    )


# ------------------------------------------------- stale job cleanup


def mark_expired_jobs(
    jobs: dict[str, AsyncJob],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Mark jobs as expired if their TTL has elapsed.

    Returns the list of job IDs that were marked expired.
    """
    current = now or datetime.now(timezone.utc)
    expired_ids: list[str] = []
    for job_id, job in jobs.items():
        if job.status in ("completed", "error", "cancelled", "expired"):
            if job.expires_at and _parse_iso(job.expires_at) < current:
                job.status = "expired"
                expired_ids.append(job_id)
    return expired_ids


def cleanup_expired_jobs(
    jobs: dict[str, AsyncJob],
    *,
    now: datetime | None = None,
) -> list[str]:
    """Remove expired jobs from the registry.

    Returns the list of removed job IDs.
    """
    expired = mark_expired_jobs(jobs, now=now)
    for job_id in expired:
        del jobs[job_id]
    return expired


def _parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 timestamp string."""
    return datetime.fromisoformat(s)


# ------------------------------------------------- concurrency guard


@dataclass
class ConcurrencyGuard:
    """Simple concurrency guard for async worker jobs.

    Parameters
    ----------
    max_concurrent_jobs:
        Maximum number of concurrently running jobs.
    reject_when_busy:
        If ``True``, reject new jobs when at capacity.
        If ``False``, allow queuing.
    """

    max_concurrent_jobs: int = 1
    reject_when_busy: bool = True
    _running_count: int = field(default=0, init=False, repr=False)

    @property
    def running_jobs(self) -> int:
        """Number of currently running jobs."""
        return self._running_count

    def can_accept(self) -> bool:
        """Check if a new job can be accepted."""
        return self._running_count < self.max_concurrent_jobs

    def try_acquire(self) -> bool:
        """Try to acquire a slot. Returns True if successful."""
        if self.can_accept():
            self._running_count += 1
            return True
        return False

    def release(self) -> None:
        """Release a slot."""
        if self._running_count > 0:
            self._running_count -= 1

    def to_dict(self) -> dict[str, Any]:
        """Return concurrency info for diagnostics."""
        return {
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "running_jobs": self._running_count,
            "reject_when_busy": self.reject_when_busy,
        }


def create_concurrency_guard_from_env() -> ConcurrencyGuard:
    """Create a concurrency guard from environment variables."""
    max_jobs = int(os.environ.get("MANGA_AUTOPILOT_MODAL_MAX_CONCURRENT_JOBS", "1"))
    reject = os.environ.get("MANGA_AUTOPILOT_MODAL_REJECT_WHEN_BUSY", "true").lower() in ("true", "1", "yes")
    return ConcurrencyGuard(max_concurrent_jobs=max_jobs, reject_when_busy=reject)


# -------------------------------------------------- diagnostics builder


def build_diagnostics(
    *,
    executor: str = "modal-comfyui",
    auth_enabled: bool = False,
    health_requires_auth: bool = False,
    timeout_policy: ModalWorkerTimeoutPolicy | None = None,
    concurrency: ConcurrencyGuard | None = None,
    artifact_mode: str = "base64",
    artifact_store: str = "local",
    artifact_access_mode: str = "public",
) -> dict[str, Any]:
    """Build a diagnostics response."""
    tp = timeout_policy or ModalWorkerTimeoutPolicy()
    cg = concurrency or ConcurrencyGuard()
    return {
        "status": "ok",
        "executor": executor,
        "auth_enabled": auth_enabled,
        "health_requires_auth": health_requires_auth,
        "timeouts": {
            "startup_timeout_sec": tp.startup_timeout_sec,
            "generation_timeout_sec": tp.generation_timeout_sec,
            "upload_timeout_sec": tp.upload_timeout_sec,
            "job_ttl_sec": tp.job_ttl_sec,
        },
        "concurrency": {
            **cg.to_dict(),
            "queued_jobs": 0,
        },
        "artifact": {
            "mode": artifact_mode,
            "store": artifact_store,
            "access_mode": artifact_access_mode,
        },
    }
