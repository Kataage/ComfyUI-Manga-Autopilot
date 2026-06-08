"""E2E tests exercising the RemoteHTTPExecutor + FakeRemoteWorker path.

These tests verify that the autopilot pipeline can drive image generation
through the :class:`RemoteHTTPExecutor` → fake remote worker HTTP path
*without* a live ComfyUI server, real GPU, or external network.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from manga_autopilot.routes import register_all
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings


# --------------------------------------------------------- fakes
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

        # Story planner
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
        # Character planner
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
        # Page planner
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
        # Panel planner
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
        # Prompt builder
        if "positive" in required:
            return json.dumps({
                "positive": "hero standing tall, wide shot, blue hair",
                "negative": "low quality, blurry",
                "seed": 12345,
                "width": 64,
                "height": 64,
            })


# ---- Fixtures ----

@pytest.fixture()
async def remote_e2e_client(aiohttp_client, tmp_path: Path):
    """E2E fixture: FakeLLMProvider + FakeRemoteWorker + RemoteHTTPExecutor.

    Sets ``manga_panel_executor_factory`` to return a RemoteHTTPExecutor
    connected to the in-process fake remote worker.
    """
    from manga_autopilot.services.remote_executor import (
        FakeRemoteWorker,
        RemoteHTTPExecutor,
        RemoteWorkerSettings,
    )

    llm = FakeLLMProvider()
    worker = FakeRemoteWorker(seed=42)

    # Start the fake remote worker as an in-process aiohttp server.
    runner = web.AppRunner(worker.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # Get the actual port assigned.
    server = site._server
    assert server is not None
    sockets = server.sockets
    assert sockets is not None
    port = sockets[0].getsockname()[1]

    settings = RemoteWorkerSettings(
        base_url=f"http://127.0.0.1:{port}",
        timeout_sec=10.0,
    )
    executor = RemoteHTTPExecutor(settings=settings)

    app = web.Application()
    app["manga_llm_provider"] = llm
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda project_id: RemoteHTTPExecutor(
        settings=settings,
        project_id=project_id,
    )

    register_all(app, storage_root=str(tmp_path))
    cli = await aiohttp_client(app)

    yield cli, tmp_path, llm, worker, executor

    await runner.cleanup()


# ---- Helpers ----

async def _wait_for_completion(cli, project_id: str, timeout: float = 15.0) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await cli.get(
            f"/manga_autopilot/api/projects/{project_id}/autopilot/status"
        )
        assert resp.status == 200
        body = await resp.json()
        last = body
        state = body.get("state", "")
        if state == "COMPLETED":
            return body
        if state in ("FAILED", "CANCELLED"):
            pytest.fail(f"autopilot reached terminal state: {state}")
        await asyncio.sleep(0.05)
    pytest.fail(f"autopilot did not complete within {timeout}s; last state={last.get('state')}")
    return last  # unreachable but satisfies type checker


async def _wait_for_terminal(cli, project_id: str, timeout: float = 15.0) -> dict[str, Any]:
    """Wait for any terminal state (COMPLETED, FAILED, CANCELLED)."""
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await cli.get(
            f"/manga_autopilot/api/projects/{project_id}/autopilot/status"
        )
        assert resp.status == 200
        body = await resp.json()
        last = body
        state = body.get("state", "")
        if state == "COMPLETED" or state.startswith("FAILED") or state == "CANCELLED":
            return body
        await asyncio.sleep(0.05)
    pytest.fail(f"autopilot did not reach terminal state within {timeout}s; last state={last.get('state')}")
    return last  # unreachable but satisfies type checker


# ---- Test ----

async def test_autopilot_can_generate_panels_with_fake_remote_executor(
    remote_e2e_client,
) -> None:
    """1-page / 1-panel autopilot through RemoteHTTPExecutor → FakeRemoteWorker."""
    cli, tmp_path, llm, worker, executor = remote_e2e_client

    # 1. Create the project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "RemoteExecutor E2E", "title": "RemoteExecutor"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "A hero standing on a cliff",
            "page_count": 1,
            "panels_per_page": 1,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=20.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. Fake remote worker received exactly one request.
    assert len(worker.requests) == 1, (
        f"expected 1 request to fake remote worker, got {len(worker.requests)}"
    )
    req = worker.requests[0]

    # 5. Request payload contains expected fields.
    assert req["project_id"] == project_id, (
        f"expected project_id={project_id!r}, got {req['project_id']!r}"
    )
    assert req["panel_id"]
    assert isinstance(req["panel_id"], str) and len(req["panel_id"]) > 0
    assert req["prompt"]
    assert isinstance(req["prompt"], str) and len(req["prompt"]) > 0
    assert req["seed"] is not None
    assert isinstance(req["seed"], int)
    assert req["width"] > 0
    assert req["height"] > 0
    assert req["workflow_id"] == "anime_t2i_default"

    # 6. Panel image was saved under assets/panels.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 1
    rec = panels[0]
    assert rec["image_path"] is not None
    assert Path(rec["image_path"]).exists()
    assert rec["status"] == "generated"

    # 7. Job was completed.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 1
    job = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert job["status"] == "completed"

    # 8. generation_log.json confirms COMPLETED.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 9. Page PNG was rendered.
    page_path = project_root / "exports" / "pages" / "page_0001.png"
    assert page_path.exists()
    assert page_path.stat().st_size > 0

    # 10. Manifest is correct.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 1
    assert manifest["stats"]["panel_count"] == 1
    assert manifest["stats"]["generated_images"] == 1


# ---- Failure fixture ----

@pytest.fixture()
async def remote_fail_e2e_client(aiohttp_client, tmp_path: Path):
    """E2E fixture where the remote worker always returns status='error'."""
    from manga_autopilot.services.remote_executor import (
        FakeRemoteWorker,
        RemoteHTTPExecutor,
        RemoteWorkerSettings,
    )

    llm = FakeLLMProvider()
    worker = FakeRemoteWorker(mode="status_error")

    runner = web.AppRunner(worker.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    sockets = server.sockets
    assert sockets is not None
    port = sockets[0].getsockname()[1]

    settings = RemoteWorkerSettings(
        base_url=f"http://127.0.0.1:{port}",
        timeout_sec=10.0,
    )

    app = web.Application()
    app["manga_llm_provider"] = llm
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda project_id: RemoteHTTPExecutor(
        settings=settings,
        project_id=project_id,
    )

    register_all(app, storage_root=str(tmp_path))
    cli = await aiohttp_client(app)

    yield cli, tmp_path, llm, worker

    await runner.cleanup()


async def test_autopilot_records_failure_when_remote_executor_fails(
    remote_fail_e2e_client,
) -> None:
    """Autopilot reaches FAILED state when remote worker returns error."""
    cli, tmp_path, llm, worker = remote_fail_e2e_client

    # 1. Create project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "RemoteFail E2E", "title": "RemoteFail"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start autopilot — remote worker will fail.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "A hero standing on a cliff",
            "page_count": 1,
            "panels_per_page": 1,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for terminal state — should be FAILED.
    final = await _wait_for_terminal(cli, project_id, timeout=20.0)
    assert final["state"].startswith("FAILED"), (
        f"expected FAILED state, got {final['state']}"
    )

    project_root = tmp_path / "projects" / project_id

    # 4. generation_log.json exists with FAILED state.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"].startswith("FAILED")

    # 5. Remote worker received the request (attempted generation).
    assert len(worker.requests) >= 1

    # 6. panels.json exists but panel is not marked as generated.
    panels_path = project_root / "panels.json"
    if panels_path.exists():
        panels = json.loads(panels_path.read_text(encoding="utf-8"))
        for rec in panels:
            # Either no image_path or status is not "generated".
            assert rec.get("status") != "generated" or rec.get("image_path") is None, (
                f"panel should not be generated when remote worker fails: {rec}"
            )

    # 7. No completed page PNG or manifest is produced.
    manifest_path = project_root / "manifest.json"
    # These may or may not exist depending on pipeline stage, but if they
    # exist the manifest should NOT say "completed".
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest.get("status") != "completed", (
            "manifest should not report completed when remote executor fails"
        )
