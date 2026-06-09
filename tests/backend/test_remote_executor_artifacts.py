"""Tests for RemoteHTTPExecutor artifact support.

Covers artifact_url and artifact_path response modes without requiring
real network services or GPU hardware.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from aiohttp import web

from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.remote_executor import (
    FakeRemoteWorker,
    RemoteExecutorImageError,
    RemoteHTTPExecutor,
    RemoteWorkerSettings,
)

# ---------------------------------------------------------------- helpers

def _parse_page_count(prompt: str) -> int:
    m = re.search(r"ページ数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


def _parse_panel_count(prompt: str) -> int:
    m = re.search(r"パネル数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


class FakeLLMProvider(LLMProvider):
    """LLM that returns canned plans matching the full autopilot pipeline."""

    def __init__(self, settings: LLMSettings | None = None) -> None:
        super().__init__(settings or LLMSettings())
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
    ) -> str:
        self.calls.append({"prompt": prompt, "schema_keys": list((schema or {}).keys())})
        required = (schema or {}).get("required", [])

        if "title" in required and "pages" in required:
            pc = _parse_page_count(prompt)
            return json.dumps({
                "title": f"Sample {pc}-Page",
                "logline": "A hero adventure.",
                "genre": "fantasy",
                "pages": [
                    {
                        "pageNumber": i + 1,
                        "summary": f"Page {i + 1} summary",
                        "emotionalGoal": "determined",
                        "visualGoal": "wide shot",
                        "panelCount": 1,
                    }
                    for i in range(pc)
                ],
            })
        if "characters" in required:
            return json.dumps({
                "characters": [
                    {
                        "id": "char_hero",
                        "name": "Hero",
                        "role": "protagonist",
                        "visualTraits": ["blue hair", "red scarf"],
                        "mustKeep": ["blue hair"],
                        "styleHints": "manga",
                    }
                ]
            })
        if "pages" in required:
            pc = _parse_page_count(prompt)
            return json.dumps({
                "pages": [
                    {
                        "pageNumber": i + 1,
                        "summary": f"Page {i + 1} summary",
                        "emotionalGoal": "determined",
                        "visualGoal": "wide shot",
                        "panelCount": 1,
                    }
                    for i in range(pc)
                ],
            })
        if "panels" in required:
            pc = _parse_panel_count(prompt)
            return json.dumps({
                "panels": [
                    {
                        "panelNumber": i + 1,
                        "purpose": f"panel {i + 1} shot",
                        "shot": "wide",
                        "cameraAngle": "low",
                        "action": "action",
                        "emotion": "determined",
                        "characters": ["char_hero"],
                        "background": "open field",
                        "visualPriority": "character",
                        "dialogue": [
                            {
                                "speaker": "Hero",
                                "text": "行くぞ",
                                "type": "speech",
                                "characterId": "char_hero",
                            }
                        ],
                    }
                    for i in range(pc)
                ],
            })
        if "positive" in required:
            return json.dumps({
                "positive": "hero standing tall, wide shot, blue hair",
                "negative": "low quality, blurry",
                "seed": 12345,
                "width": 64,
                "height": 64,
            })
        return json.dumps({})


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
    worker._server_port = port
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


# ---------------------------------------------------------------- artifact_url tests

async def test_remote_executor_downloads_artifact_url() -> None:
    """Executor downloads image from artifact_url when image_base64 is absent."""
    worker = FakeRemoteWorker(mode="artifact_url", seed=55)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings, project_id="proj_001")
        result = await executor.submit(
            prompt=_make_prompt(seed=55),
            workflow_id="anime_t2i_default",
            seed=55,
            candidate_id="panel_001_c00",
        )
        assert result.image is not None
        assert result.image.size == (64, 64)
        assert result.candidate_id == "panel_001_c00"
        assert len(worker.requests) == 1
        assert len(worker.artifacts) == 1
    finally:
        await runner.cleanup()


async def test_remote_executor_reads_artifact_path() -> None:
    """Executor reads image from artifact_path when image_base64 is absent."""
    worker = FakeRemoteWorker(mode="artifact_path", seed=66)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings, project_id="proj_001")
        result = await executor.submit(
            prompt=_make_prompt(seed=66),
            workflow_id="anime_t2i_default",
            seed=66,
            candidate_id="panel_002_c00",
        )
        assert result.image is not None
        assert result.image.size == (64, 64)
        assert result.candidate_id == "panel_002_c00"
        assert len(worker.requests) == 1
    finally:
        await runner.cleanup()


async def test_remote_executor_polls_until_async_artifact_url_completed() -> None:
    """Async artifact_url job goes through queued → running → completed."""
    worker = FakeRemoteWorker(mode="async_artifact_url", seed=77)
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
            candidate_id="panel_003_c00",
        )
        assert result.image is not None
        assert result.image.size == (64, 64)
        assert result.candidate_id == "panel_003_c00"
        assert len(worker.requests) == 1
        assert len(worker.job_requests) >= 2
        assert len(worker.artifacts) == 1
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_artifact_url_404() -> None:
    """Executor raises error when artifact_url returns 404."""
    worker = FakeRemoteWorker(mode="artifact_url_404", seed=88)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings, project_id="proj_001")
        with pytest.raises(Exception, match="HTTP 404"):
            await executor.submit(
                prompt=_make_prompt(seed=88),
                workflow_id="anime_t2i_default",
                seed=88,
                candidate_id="panel_004_c00",
            )
        assert len(worker.requests) == 1
    finally:
        await runner.cleanup()


async def test_remote_executor_raises_on_missing_artifact_path() -> None:
    """Executor raises error when artifact_path does not exist."""
    worker = FakeRemoteWorker(mode="artifact_path_missing", seed=99)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings, project_id="proj_001")
        with pytest.raises(RemoteExecutorImageError, match="does not exist"):
            await executor.submit(
                prompt=_make_prompt(seed=99),
                workflow_id="anime_t2i_default",
                seed=99,
                candidate_id="panel_005_c00",
            )
        assert len(worker.requests) == 1
    finally:
        await runner.cleanup()


async def test_autopilot_can_generate_panels_with_artifact_url_remote_executor() -> None:
    """Full autopilot pipeline works with artifact_url remote executor."""
    from aiohttp import web as _web

    from manga_autopilot.routes import register_all

    worker = FakeRemoteWorker(mode="artifact_url", seed=101)
    runner, port = await _start_worker(worker)
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")

        app = _web.Application()
        app["manga_panel_executor_factory"] = lambda project_id: RemoteHTTPExecutor(
            settings=settings, project_id=project_id,
        )

        app["manga_llm_provider"] = FakeLLMProvider()
        app["manga_default_workflow_id"] = "anime_t2i_default"

        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            register_all(app, storage_root=tmp_dir)

            test_runner = _web.AppRunner(app)
            await test_runner.setup()
            site = _web.TCPSite(test_runner, "127.0.0.1", 0)
            await site.start()
            srv = site._server
            assert srv is not None
            test_port = srv.sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{test_port}"

            import aiohttp
            async with aiohttp.ClientSession() as session:
                # Create project
                async with session.post(
                    f"{base}/manga_autopilot/api/projects",
                    json={"name": "Artifact Test"},
                ) as resp:
                    assert resp.status == 201
                    proj = await resp.json()
                    project_id = proj["id"]

                # Start autopilot
                async with session.post(
                    f"{base}/manga_autopilot/api/projects/{project_id}/autopilot/start",
                    json={
                        "idea": "A girl standing on a cliff",
                        "page_count": 1,
                        "panels_per_page": 1,
                        "candidate_count": 1,
                        "max_retries": 0,
                    },
                ) as resp:
                    assert resp.status == 202

                # Poll until completed
                for _ in range(50):
                    async with session.get(
                        f"{base}/manga_autopilot/api/projects/{project_id}/autopilot/status",
                    ) as resp:
                        status_data = await resp.json()
                        if status_data["state"] == "COMPLETED":
                            break
                    import asyncio
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("autopilot did not complete")

                # Verify result
                assert status_data["state"] == "COMPLETED"

            await test_runner.cleanup()
    finally:
        await runner.cleanup()
