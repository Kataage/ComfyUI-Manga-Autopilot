"""Unit tests for RemoteHTTPExecutor error handling.

Tests cover every failure mode of the remote executor without requiring
real network services or GPU hardware.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.remote_executor import (
    FakeRemoteWorker,
    RemoteExecutorHTTPError,
    RemoteExecutorImageError,
    RemoteExecutorResponseError,
    RemoteExecutorTimeoutError,
    RemoteHTTPExecutor,
    RemoteWorkerSettings,
)

# ---------------------------------------------------------------- helpers

async def _start_worker(
    worker: FakeRemoteWorker,
) -> tuple[web.AppRunner, int]:
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

async def test_remote_executor_sends_authorization_header() -> None:
    """Authorization header is sent when api_key is set."""
    worker = FakeRemoteWorker(mode="success")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(
            base_url=f"http://127.0.0.1:{port}",
            api_key="secret-token",
        )
        executor = RemoteHTTPExecutor(settings=settings)
        result = await executor.submit(
            prompt=_make_prompt(),
            workflow_id="test_wf",
            seed=1,
            candidate_id="cand_001",
        )
        assert result.image is not None
        assert len(worker.headers) == 1
        assert worker.headers[0].get("Authorization") == "Bearer secret-token"
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_http_500() -> None:
    """HTTP 500 raises RemoteExecutorHTTPError."""
    worker = FakeRemoteWorker(mode="http_500")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorHTTPError) as exc_info:
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
        assert exc_info.value.status == 500
        assert "internal server error" in str(exc_info.value)
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_status_error() -> None:
    """Worker status='error' raises RemoteExecutorResponseError."""
    worker = FakeRemoteWorker(mode="status_error")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorResponseError, match="model not found"):
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_invalid_json() -> None:
    """Non-JSON response raises RemoteExecutorResponseError."""
    worker = FakeRemoteWorker(mode="invalid_json")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorResponseError, match="invalid JSON"):
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_missing_image_base64() -> None:
    """Response without image_base64 raises RemoteExecutorResponseError."""
    worker = FakeRemoteWorker(mode="missing_image")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorResponseError, match="no image_base64"):
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_invalid_base64() -> None:
    """Non-base64 image_base64 raises RemoteExecutorImageError."""
    worker = FakeRemoteWorker(mode="invalid_base64")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorImageError, match="invalid base64"):
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_invalid_image_bytes() -> None:
    """Valid base64 but not an image raises RemoteExecutorImageError."""
    worker = FakeRemoteWorker(mode="invalid_image")
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorImageError, match="invalid image bytes"):
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_timeout() -> None:
    """Timeout raises RemoteExecutorTimeoutError."""
    worker = FakeRemoteWorker(mode="timeout", delay_sec=5.0)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(
            base_url=f"http://127.0.0.1:{port}",
            timeout_sec=0.1,
        )
        executor = RemoteHTTPExecutor(settings=settings)
        with pytest.raises(RemoteExecutorTimeoutError) as exc_info:
            await executor.submit(
                prompt=_make_prompt(),
                workflow_id="test_wf",
                seed=1,
                candidate_id="cand_001",
            )
        assert exc_info.value.timeout_sec == 0.1
    finally:
        await runner.cleanup()


async def test_remote_executor_success_path() -> None:
    """Success path returns valid image and records request."""
    worker = FakeRemoteWorker(mode="success", seed=99)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings, project_id="proj_001")
        result = await executor.submit(
            prompt=_make_prompt(seed=99),
            workflow_id="anime_t2i_default",
            seed=99,
            candidate_id="panel_001_c00",
        )
        assert result.image is not None
        assert result.image.size == (64, 64)
        assert result.candidate_id == "panel_001_c00"
        assert result.workflow_id == "anime_t2i_default"

        assert len(worker.requests) == 1
        req = worker.requests[0]
        assert req["project_id"] == "proj_001"
        assert req["panel_id"] == "panel_001_c00"
        assert req["seed"] == 99
    finally:
        await runner.cleanup()
