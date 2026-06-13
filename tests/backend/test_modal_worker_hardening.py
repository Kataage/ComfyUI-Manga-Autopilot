"""Contract tests for Modal worker production hardening."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from manga_autopilot.services.modal_worker_hardening import (  # noqa: E402
    AsyncJob,
    ConcurrencyGuard,
    ModalWorkerTimeoutPolicy,
    build_diagnostics,
    build_structured_error,
    check_bearer_token,
    cleanup_expired_jobs,
    create_async_job,
    create_auth_config_from_env,
    create_concurrency_guard_from_env,
    create_timeout_policy_from_env,
    is_auth_required,
    mark_expired_jobs,
)

# --------------------------------------------------------------- auth tests


class TestBearerToken:
    def test_allows_request_when_token_unset(self):
        assert check_bearer_token(None, "") is True
        assert check_bearer_token("Bearer abc", "") is True

    def test_rejects_missing_bearer_token_when_configured(self):
        assert check_bearer_token(None, "secret") is False

    def test_rejects_invalid_token(self):
        assert check_bearer_token("Bearer wrong", "secret") is False

    def test_accepts_valid_token(self):
        assert check_bearer_token("Bearer secret", "secret") is True

    def test_rejects_non_bearer_scheme(self):
        assert check_bearer_token("Basic dXNlcjpwYXNz", "secret") is False

    def test_case_insensitive_bearer(self):
        assert check_bearer_token("bearer secret", "secret") is True


class TestIsAuthRequired:
    def test_no_token_means_no_auth(self):
        assert is_auth_required(expected_token="", health_requires_auth=False, path="/v1/health") is False

    def test_health_endpoint_no_auth_when_disabled(self):
        assert is_auth_required(expected_token="tok", health_requires_auth=False, path="/v1/health") is False

    def test_health_endpoint_auth_when_enabled(self):
        assert is_auth_required(expected_token="tok", health_requires_auth=True, path="/v1/health") is True

    def test_generate_panel_requires_auth(self):
        assert is_auth_required(expected_token="tok", health_requires_auth=False, path="/v1/generate-panel") is True

    def test_diagnostics_no_auth_when_disabled(self):
        assert is_auth_required(expected_token="tok", health_requires_auth=False, path="/v1/diagnostics") is False


class TestAuthConfigFromEnv:
    def test_default_no_token(self):
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("MANGA_AUTOPILOT_MODAL_WORKER_TOKEN") and not k.startswith("MANGA_AUTOPILOT_MODAL_HEALTH_REQUIRES_AUTH")}
        import unittest.mock
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            token, health_auth = create_auth_config_from_env()
            assert token == ""
            assert health_auth is False

    def test_reads_token(self):
        import os
        import unittest.mock
        env = {**os.environ, "MANGA_AUTOPILOT_MODAL_WORKER_TOKEN": "mytoken"}
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            token, _ = create_auth_config_from_env()
            assert token == "mytoken"


# ----------------------------------------------------------- timeout tests


class TestTimeoutPolicy:
    def test_loads_defaults(self):
        policy = ModalWorkerTimeoutPolicy()
        assert policy.startup_timeout_sec == 120
        assert policy.generation_timeout_sec == 600
        assert policy.upload_timeout_sec == 120
        assert policy.job_ttl_sec == 3600

    def test_rejects_invalid_values(self):
        with pytest.raises(ValueError, match="must be > 0"):
            ModalWorkerTimeoutPolicy(startup_timeout_sec=0)
        with pytest.raises(ValueError, match="must be > 0"):
            ModalWorkerTimeoutPolicy(generation_timeout_sec=-1)

    def test_custom_values(self):
        policy = ModalWorkerTimeoutPolicy(
            startup_timeout_sec=30,
            generation_timeout_sec=1200,
            upload_timeout_sec=60,
            job_ttl_sec=7200,
        )
        assert policy.startup_timeout_sec == 30
        assert policy.generation_timeout_sec == 1200

    def test_from_env(self):
        import os
        import unittest.mock
        env = {
            **os.environ,
            "MANGA_AUTOPILOT_MODAL_STARTUP_TIMEOUT_SEC": "60",
            "MANGA_AUTOPILOT_MODAL_GENERATION_TIMEOUT_SEC": "300",
        }
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            policy = create_timeout_policy_from_env()
            assert policy.startup_timeout_sec == 60
            assert policy.generation_timeout_sec == 300
            assert policy.upload_timeout_sec == 120  # default


# ----------------------------------------------------------- error response


class TestStructuredError:
    def test_error_response_contract(self):
        resp = build_structured_error(
            "checkpoint not found: anima.safetensors",
            error_code="CHECKPOINT_NOT_FOUND",
            executor="modal-comfyui",
            metadata={"run_id": "run_123"},
        )
        assert resp["status"] == "error"
        assert resp["error_code"] == "CHECKPOINT_NOT_FOUND"
        assert resp["error"] == "checkpoint not found: anima.safetensors"
        assert resp["metadata"]["executor"] == "modal-comfyui"
        assert resp["metadata"]["run_id"] == "run_123"

    def test_error_codes_all_defined(self):
        from manga_autopilot.services.modal_worker_hardening import ERROR_CODES
        expected = {
            "UNAUTHORIZED", "WORKER_BUSY", "STARTUP_TIMEOUT",
            "GENERATION_TIMEOUT", "UPLOAD_TIMEOUT", "CHECKPOINT_NOT_FOUND",
            "WORKFLOW_INVALID", "COMFYUI_NOT_READY", "ARTIFACT_UPLOAD_FAILED",
            "JOB_NOT_FOUND", "JOB_EXPIRED", "JOB_CANCELLED", "UNKNOWN_ERROR",
        }
        assert expected == set(ERROR_CODES.keys())


# ----------------------------------------------------------- async job tests


class TestAsyncJob:
    def test_metadata_includes_timestamps_and_expires_at(self):
        job = create_async_job("job_001", job_ttl_sec=3600)
        assert job.job_id == "job_001"
        assert job.status == "queued"
        assert job.created_at is not None
        assert job.started_at is None
        assert job.completed_at is None
        assert job.expires_at is not None

    def test_to_dict(self):
        job = create_async_job("job_002", job_ttl_sec=600, metadata={"run_id": "r1"})
        d = job.to_dict()
        assert d["job_id"] == "job_002"
        assert d["status"] == "queued"
        assert d["expires_at"] is not None
        assert d["metadata"]["run_id"] == "r1"

    def test_ttl_marks_old_jobs_expired(self):
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        jobs: dict[str, AsyncJob] = {}
        job = create_async_job("j1", job_ttl_sec=100, now=now)
        job.status = "completed"
        jobs["j1"] = job

        # Simulate 200 seconds later
        later = now + timedelta(seconds=200)
        expired = mark_expired_jobs(jobs, now=later)
        assert "j1" in expired
        assert jobs["j1"].status == "expired"

    def test_cleanup_removes_stale_completed_jobs(self):
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        jobs: dict[str, AsyncJob] = {}
        job = create_async_job("j1", job_ttl_sec=100, now=now)
        job.status = "completed"
        jobs["j1"] = job

        later = now + timedelta(seconds=200)
        removed = cleanup_expired_jobs(jobs, now=later)
        assert "j1" in removed
        assert "j1" not in jobs

    def test_running_jobs_not_expired_by_default(self):
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        jobs: dict[str, AsyncJob] = {}
        job = create_async_job("j1", job_ttl_sec=100, now=now)
        job.status = "running"
        jobs["j1"] = job

        later = now + timedelta(seconds=200)
        expired = mark_expired_jobs(jobs, now=later)
        assert "j1" not in expired
        assert jobs["j1"].status == "running"


# --------------------------------------------------------- concurrency tests


class TestConcurrencyGuard:
    def test_rejects_when_busy(self):
        guard = ConcurrencyGuard(max_concurrent_jobs=1, reject_when_busy=True)
        assert guard.try_acquire() is True
        assert guard.try_acquire() is False
        assert guard.running_jobs == 1
        guard.release()
        assert guard.try_acquire() is True

    def test_allows_queuing_when_not_rejecting(self):
        guard = ConcurrencyGuard(max_concurrent_jobs=1, reject_when_busy=False)
        assert guard.try_acquire() is True
        # can_accept returns False but we don't check here
        assert guard.running_jobs == 1
        guard.release()

    def test_to_dict(self):
        guard = ConcurrencyGuard(max_concurrent_jobs=2)
        d = guard.to_dict()
        assert d["max_concurrent_jobs"] == 2
        assert d["running_jobs"] == 0
        assert d["reject_when_busy"] is True

    def test_from_env(self):
        import os
        import unittest.mock
        env = {
            **os.environ,
            "MANGA_AUTOPILOT_MODAL_MAX_CONCURRENT_JOBS": "4",
            "MANGA_AUTOPILOT_MODAL_REJECT_WHEN_BUSY": "false",
        }
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            guard = create_concurrency_guard_from_env()
            assert guard.max_concurrent_jobs == 4
            assert guard.reject_when_busy is False


# --------------------------------------------------------- diagnostics tests


class TestDiagnostics:
    def test_contains_all_sections(self):
        tp = ModalWorkerTimeoutPolicy()
        cg = ConcurrencyGuard(max_concurrent_jobs=2)
        diag = build_diagnostics(
            auth_enabled=True,
            health_requires_auth=False,
            timeout_policy=tp,
            concurrency=cg,
            artifact_mode="signed",
            artifact_store="s3",
            artifact_access_mode="signed",
        )
        assert diag["status"] == "ok"
        assert diag["executor"] == "modal-comfyui"
        assert diag["auth_enabled"] is True
        assert "timeouts" in diag
        assert "concurrency" in diag
        assert "artifact" in diag
        assert diag["timeouts"]["startup_timeout_sec"] == 120
        assert diag["concurrency"]["max_concurrent_jobs"] == 2
        assert diag["artifact"]["mode"] == "signed"
        assert diag["artifact"]["store"] == "s3"
        assert diag["artifact"]["access_mode"] == "signed"
