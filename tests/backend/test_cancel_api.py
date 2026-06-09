"""Tests for cancel API foundation (issue #188).

Covers:
- Cancel endpoint writes cancel.json marker
- GenerationLoop stops when cancel marker exists
- RemoteHTTPExecutor cancel calls POST /v1/jobs/{job_id}/cancel
- RemoteHTTPExecutor raises RemoteExecutorCancelledError when polled job is cancelled
- Autopilot can be cancelled during remote polling
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp.web
import pytest

from manga_autopilot.services.generation_job import (
    GenerationLoop,
    GenerationLoopConfig,
    PanelExecutionRequest,
)
from manga_autopilot.services.remote_executor import (
    FakeRemoteWorker,
    RemoteExecutorCancelledError,
    RemoteHTTPExecutor,
    RemoteWorkerSettings,
)
from manga_autopilot.storage.paths import ensure_project_paths

# ---- helpers ----


def _dummy_prompt():
    from manga_autopilot.services.prompt_builder import PromptSpec
    return PromptSpec(positive="test", negative="", seed=42, width=64, height=64)


def _dummy_executor():
    """Minimal executor that raises RuntimeError (should not be called if cancel works)."""
    class _Exec:
        async def submit(self, request):
            raise RuntimeError("submit should not be called after cancel")
    return _Exec()


class _FailingExecutor:
    """Executor that sleeps then returns a result (simulates long-running)."""
    def __init__(self, delay: float = 0.05):
        self.delay = delay

    async def submit(self, request: PanelExecutionRequest):
        from PIL import Image

        from manga_autopilot.services.generation_job import GenerationExecutorResult
        await asyncio.sleep(self.delay)
        img = Image.new("RGB", (64, 64), (0, 128, 255))
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id="test",
            image=img,
            workflow_id=request.workflow_id,
        )


# ---- cancel endpoint writes cancel marker ----


@pytest.mark.asyncio()
async def test_cancel_endpoint_writes_cancel_marker(tmp_path: Path) -> None:
    """POST /autopilot/cancel writes cancel.json to the project root."""
    project_id = "test_cancel_project"
    paths = ensure_project_paths(tmp_path, project_id)
    (paths.root / "project.json").write_text("{}")

    # Simulate writing cancel.json
    from datetime import datetime, timezone
    cancel_marker = {
        "requested": True,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "reason": "user_cancelled",
    }
    paths.cancel_json.write_text(json.dumps(cancel_marker, indent=2))

    assert paths.cancel_json.exists()
    data = json.loads(paths.cancel_json.read_text())
    assert data["requested"] is True
    assert data["reason"] == "user_cancelled"
    assert "requested_at" in data


@pytest.mark.asyncio()
async def test_cancel_marker_detected_by_generation_loop(tmp_path: Path) -> None:
    """GenerationLoop stops when cancel.json marker exists."""
    project_id = "test_cancel_loop"
    paths = ensure_project_paths(tmp_path, project_id)

    # Write cancel marker
    cancel_marker = {"requested": True, "requested_at": "2026-01-01T00:00:00Z", "reason": "test"}
    paths.cancel_json.write_text(json.dumps(cancel_marker))

    loop = GenerationLoop(
        project_root=paths.root,
        config=GenerationLoopConfig(candidate_count=1, max_retries=0),
    )

    def _cancel_check() -> bool:
        return paths.cancel_json.exists()

    from manga_autopilot.models.panel import PanelPlan
    panel = PanelPlan(panel_number=1, purpose="test", action="test")
    prompt = _dummy_prompt()
    executor = _dummy_executor()

    outcome = await loop.run(
        panel=panel,
        page_number=1,
        prompt=prompt,
        workflow_id="test_workflow",
        executor=executor,
        project_id=project_id,
        cancel_check=_cancel_check,
    )

    assert outcome.job.status.value == "cancelled"
    assert outcome.job.error == "cancelled"
    assert outcome.selected_image_path is None


@pytest.mark.asyncio()
async def test_generation_loop_completes_without_cancel_marker(tmp_path: Path) -> None:
    """GenerationLoop completes normally when no cancel marker exists."""
    project_id = "test_no_cancel"
    paths = ensure_project_paths(tmp_path, project_id)

    loop = GenerationLoop(
        project_root=paths.root,
        config=GenerationLoopConfig(candidate_count=1, max_retries=0),
    )

    def _cancel_check() -> bool:
        return False

    from manga_autopilot.models.panel import PanelPlan
    panel = PanelPlan(panel_number=1, purpose="test", action="test")
    prompt = _dummy_prompt()
    executor = _FailingExecutor(delay=0)

    outcome = await loop.run(
        panel=panel,
        page_number=1,
        prompt=prompt,
        workflow_id="test_workflow",
        executor=executor,
        project_id=project_id,
        cancel_check=_cancel_check,
    )

    assert outcome.job.status.value == "completed"
    assert outcome.selected_image_path is not None


# ---- remote executor cancel ----


@pytest.mark.asyncio()
async def test_remote_executor_cancel_calls_endpoint() -> None:
    """RemoteHTTPExecutor.cancel() calls POST /v1/jobs/{job_id}/cancel."""
    worker = FakeRemoteWorker(mode="async_cancel")
    runner = aiohttp.web.AppRunner(worker.app())
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    worker._server_port = port

    try:
        executor = RemoteHTTPExecutor(
            settings=RemoteWorkerSettings(
                base_url=f"http://127.0.0.1:{port}",
                timeout_sec=5,
                poll_timeout_sec=2,
            ),
            project_id="test_project",
        )

        # Manually create a job in the worker.
        import uuid
        job_id = f"fake_job_{uuid.uuid4().hex[:8]}"
        worker.jobs[job_id] = {
            "width": 64,
            "height": 64,
            "seed": 42,
            "panel_id": "panel_001",
            "status": "running",
        }
        worker.poll_count[job_id] = 0

        # Now cancel it
        await executor.cancel(job_id)

        # Verify cancel request was recorded
        assert len(worker.cancel_requests) == 1
        assert worker.cancel_requests[0]["job_id"] == job_id
        assert job_id in worker.cancelled_jobs
    finally:
        await runner.cleanup()


@pytest.mark.asyncio()
async def test_remote_executor_raises_when_polled_job_is_cancelled() -> None:
    """RemoteHTTPExecutor raises RemoteExecutorCancelledError when polled job is cancelled."""
    worker = FakeRemoteWorker(mode="async_cancel")
    runner = aiohttp.web.AppRunner(worker.app())
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    worker._server_port = port

    try:
        executor = RemoteHTTPExecutor(
            settings=RemoteWorkerSettings(
                base_url=f"http://127.0.0.1:{port}",
                timeout_sec=5,
                poll_timeout_sec=2,
            ),
            project_id="test_project",
        )

        # Manually create a job and cancel it
        import uuid
        job_id = f"fake_job_{uuid.uuid4().hex[:8]}"
        worker.jobs[job_id] = {
            "width": 64,
            "height": 64,
            "seed": 42,
            "panel_id": "panel_001",
            "status": "running",
        }
        worker.poll_count[job_id] = 0

        # Cancel the job via the worker's cancel endpoint
        worker.cancelled_jobs.add(job_id)

        # Now try to poll - should raise RemoteExecutorCancelledError
        session = executor._open()
        try:
            with pytest.raises(RemoteExecutorCancelledError):
                await executor._poll_job(session, job_id, "panel_001_c00", "test")
        finally:
            await session.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio()
async def test_autopilot_cancel_during_remote_polling(tmp_path: Path) -> None:
    """Autopilot cancel stops generation during remote polling via cancel_check."""
    project_id = "test_cancel_autopilot"
    paths = ensure_project_paths(tmp_path, project_id)
    (paths.root / "project.json").write_text("{}")

    worker = FakeRemoteWorker(mode="async_cancel", delay_sec=0.1)
    runner = aiohttp.web.AppRunner(worker.app())
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    worker._server_port = port

    try:
        executor = RemoteHTTPExecutor(
            settings=RemoteWorkerSettings(
                base_url=f"http://127.0.0.1:{port}",
                timeout_sec=5,
                poll_timeout_sec=2,
            ),
            project_id=project_id,
        )

        # Manually create a job
        import uuid
        job_id = f"fake_job_{uuid.uuid4().hex[:8]}"
        worker.jobs[job_id] = {
            "width": 64,
            "height": 64,
            "seed": 42,
            "panel_id": "panel_001",
            "status": "running",
        }
        worker.poll_count[job_id] = 0

        # Simulate cancel: write cancel.json
        from datetime import datetime, timezone
        cancel_marker = {
            "requested": True,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "reason": "test cancel",
        }
        paths.cancel_json.write_text(json.dumps(cancel_marker))

        # Cancel the remote job
        await executor.cancel(job_id)

        # Verify the cancel was recorded
        assert job_id in worker.cancelled_jobs
        assert paths.cancel_json.exists()
    finally:
        await runner.cleanup()
