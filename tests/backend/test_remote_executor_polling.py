"""Tests for RemoteHTTPExecutor async job polling.

Tests cover the async polling path without requiring real network
services or GPU hardware.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.remote_executor import (
    FakeRemoteWorker,
    RemoteExecutorJobError,
    RemoteExecutorPollingTimeoutError,
    RemoteHTTPExecutor,
    RemoteWorkerSettings,
)

# ---------------------------------------------------------------- helpers

async def _start_worker(worker: FakeRemoteWorker) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(worker.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    sockets = server.sockets
    assert sockets is not None
    port = sockets[0].getsockname()[1]
    return runner, port


def _make_prompt(**overrides: Any) -> PromptSpec:
    defaults = dict(
        positive="1girl, masterpiece",
        negative="lowres, blurry",
        seed=42,
        width=64,
        height=64,
        steps=4,
        cfg=7.0,
    )
    defaults.update(overrides)
    return PromptSpec(**defaults)


# ---------------------------------------------------------------- tests

async def test_remote_executor_polls_until_async_job_completed() -> None:
    """Async job goes through queued → running → completed."""
    worker = FakeRemoteWorker(mode="async_success", seed=77)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(
            base_url=f"http://127.0.0.1:{port}",
            poll_interval_sec=0.01,
            poll_timeout_sec=5.0,
        )
        executor = RemoteHTTPExecutor(settings=settings, project_id="proj_001")
        result = await executor.submit(
            prompt=_make_prompt(seed=77),
            workflow_id="anime_t2i_default",
            seed=77,
            candidate_id="panel_001_c00",
        )
        assert result.image is not None
        assert result.image.size == (64, 64)
        assert result.candidate_id == "panel_001_c00"

        # POST was called once.
        assert len(worker.requests) == 1
        # GET was polled at least twice (running → completed).
        assert len(worker.job_requests) >= 2
        assert worker.job_requests[0]["job_id"] == worker.job_requests[1]["job_id"]
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_when_async_job_errors() -> None:
    """Async job that reaches error state raises RemoteExecutorJobError."""
    worker = FakeRemoteWorker(mode="async_error")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(
            base_url=f"http://127.0.0.1:{port}",
            poll_interval_sec=0.01,
            poll_timeout_sec=5.0,
        )
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorJobError, match="model failed"):
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
        # POST + at least 2 polls.
        assert len(worker.requests) == 1
        assert len(worker.job_requests) >= 2
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_when_async_job_polling_times_out() -> None:
    """Polling that never completes raises RemoteExecutorPollingTimeoutError."""
    worker = FakeRemoteWorker(mode="async_timeout")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(
            base_url=f"http://127.0.0.1:{port}",
            poll_interval_sec=0.01,
            poll_timeout_sec=0.1,
        )
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorPollingTimeoutError) as exc_info:
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
        assert exc_info.value.job_id is not None
        assert exc_info.value.timeout_sec == 0.1
    finally:
        await runner.cleanup()


async def test_remote_executor_max_poll_attempts() -> None:
    """max_poll_attempts limits the number of polls."""
    worker = FakeRemoteWorker(mode="async_timeout")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(
            base_url=f"http://127.0.0.1:{port}",
            poll_interval_sec=0.01,
            poll_timeout_sec=60.0,
            max_poll_attempts=3,
        )
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorPollingTimeoutError):
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
        # POST + 3 polls (max_poll_attempts + 1 because of the > check).
        assert len(worker.job_requests) == 3
    finally:
        await runner.cleanup()


async def test_sync_completed_still_works() -> None:
    """Synchronous completed path still works after async additions."""
    worker = FakeRemoteWorker(mode="success", seed=42)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(
            base_url=f"http://127.0.0.1:{port}",
        )
        executor = RemoteHTTPExecutor(settings=settings)
        result = await executor.submit(
            prompt=_make_prompt(),
            workflow_id="test_wf",
            seed=42,
            candidate_id="cand_001",
        )
        assert result.image is not None
        assert len(worker.requests) == 1
        assert len(worker.job_requests) == 0  # no polling for sync path
    finally:
        await runner.cleanup()
