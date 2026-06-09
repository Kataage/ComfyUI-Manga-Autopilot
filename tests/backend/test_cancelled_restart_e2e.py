"""Tests for cancelled project restart (issue #190).

Covers:
- Restart endpoint clears cancel marker
- Restart endpoint returns 404 for missing project
- Restart endpoint restores input from project.json
- Restart endpoint applies request body overrides
- Cancelled → restart → completed E2E
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from manga_autopilot.routes import register_all
from manga_autopilot.services.generation_job import (
    GenerationExecutorResult,
    PanelExecutionRequest,
)
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.storage.paths import ensure_project_paths

# ---- helpers ----


def _create_project(storage_root: Path, project_id: str) -> None:
    """Create a minimal project.json for testing."""
    paths = ensure_project_paths(storage_root, project_id)
    project_data = {
        "id": project_id,
        "name": f"Test Project {project_id}",
        "title": f"Test Title {project_id}",
        "settings": {
            "page_count": 1,
            "format": ["png_pages"],
            "generation": {
                "candidate_count": 1,
                "max_retry_per_panel": 0,
                "quality_threshold": 0.5,
            },
        },
    }
    paths.project_json.write_text(json.dumps(project_data, indent=2))


def _write_cancel_marker(storage_root: Path, project_id: str) -> Path:
    """Write a cancel marker and return its path."""
    paths = ensure_project_paths(storage_root, project_id)
    cancel_marker = {
        "requested": True,
        "requested_at": "2026-01-01T00:00:00Z",
        "reason": "test cancellation",
    }
    paths.cancel_json.write_text(json.dumps(cancel_marker, indent=2))
    return paths.cancel_json


# Reuse the proven LLM and executor from the existing E2E tests.


def _parse_panel_count(prompt: str) -> int:
    import re
    m = re.search(r"パネル数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


def _parse_page_number(prompt: str) -> int:
    import re
    m = re.search(r'"?(?:pageNumber|page_number)"?\s*[：:]\s*(\d+)', prompt)
    return int(m.group(1)) if m else 1


class FakeLLMProvider(LLMProvider):
    """LLM that returns canned plans matching the real schema expectations."""

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
            return json.dumps({
                "title": "Sample 1-Page",
                "logline": "A hero adventure.",
                "genre": "fantasy",
                "pages": [{"pageNumber": 1, "summary": "Page 1", "emotionalGoal": "determined", "visualGoal": "wide shot", "panelCount": 1}],
            })
        if (schema or {}).get("required") and "characters" in (schema or {}).get("required", []):
            return json.dumps({
                "characters": [{"id": "char_hero", "name": "Hero", "role": "protagonist", "visualTraits": ["blue hair"], "mustKeep": ["blue hair"], "styleHints": "manga"}]
            })
        if (schema or {}).get("required") and "pages" in (schema or {}).get("required", []):
            return json.dumps({
                "pages": [{"pageNumber": 1, "summary": "Page 1", "emotionalGoal": "determined", "visualGoal": "wide shot", "panelCount": 1}]
            })
        if (schema or {}).get("required") and "panels" in (schema or {}).get("required", []):
            return json.dumps({
                "panels": [{"panelNumber": 1, "purpose": "panel 1 shot", "shot": "wide", "cameraAngle": "low", "action": "action", "emotion": "determined", "characters": ["char_hero"], "background": "open field", "visualPriority": "character", "dialogue": [{"speaker": "Hero", "text": "行くぞ", "type": "speech", "characterId": "char_hero"}]}]
            })
        if (schema or {}).get("required") and "positive" in (schema or {}).get("required", []):
            return json.dumps({
                "positive": "hero standing tall, wide shot, blue hair",
                "negative": "low quality, blurry",
                "seed": 12345,
                "width": 64,
                "height": 64,
            })
        return "{}"


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def submit(self, request: PanelExecutionRequest):
        from PIL import Image
        self.calls.append(request)
        img = Image.new("RGB", (request.effective_width, request.effective_height), (request.seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id=f"prompt_{request.candidate_id}",
            image=img,
            workflow_id=request.workflow_id,
        )


class SlowFakeExecutor(FakeExecutor):
    """Executor that sleeps before completing, giving cancel time to arrive."""

    async def submit(self, request: PanelExecutionRequest):
        await asyncio.sleep(0.5)
        return await super().submit(request)


# ---- restart endpoint tests ----


@pytest.mark.asyncio()
async def test_restart_endpoint_clears_cancel_marker(
    aiohttp_client, tmp_path: Path,
) -> None:
    """POST /autopilot/restart deletes cancel.json."""
    project_id = "test_restart_clear"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _create_project(storage_root, project_id)
    cancel_path = _write_cancel_marker(storage_root, project_id)
    assert cancel_path.exists()

    from aiohttp import web

    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: FakeExecutor()
    register_all(app, storage_root=str(storage_root))

    client = await aiohttp_client(app)
    r = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/restart",
        json={"reason": "test restart"},
    )
    assert r.status == 202
    data = await r.json()
    assert data["project_id"] == project_id
    assert data["restarted"] is True

    # Cancel marker should be deleted
    assert not cancel_path.exists()


@pytest.mark.asyncio()
async def test_restart_endpoint_returns_404_for_missing_project(
    aiohttp_client, tmp_path: Path,
) -> None:
    """POST /autopilot/restart returns 404 for non-existent project."""
    project_id = "nonexistent_project"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()

    from aiohttp import web

    app = web.Application()
    register_all(app, storage_root=str(storage_root))

    client = await aiohttp_client(app)
    r = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/restart",
        json={"reason": "test"},
    )
    assert r.status == 404


@pytest.mark.asyncio()
async def test_restart_restores_input_from_project_json(
    aiohttp_client, tmp_path: Path,
) -> None:
    """POST /autopilot/restart restores input from project.json settings."""
    project_id = "test_restart_input"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _create_project(storage_root, project_id)
    _write_cancel_marker(storage_root, project_id)

    from aiohttp import web

    from manga_autopilot.services.autopilot import AutopilotController

    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: FakeExecutor()
    ctrl = AutopilotController()
    app["manga_autopilot_controller"] = ctrl
    register_all(app, storage_root=str(storage_root))

    client = await aiohttp_client(app)
    r = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/restart",
        json={"reason": "test restart input"},
    )
    assert r.status == 202

    # Verify the run was created with restored input
    run = ctrl.runs.get(project_id)
    assert run is not None
    assert run.input.get("page_count") == 1
    assert run.input.get("candidate_count") == 1
    assert run.input.get("max_retries") == 0
    assert run.input.get("threshold") == 0.5
    assert run.input.get("title") == f"Test Title {project_id}"


@pytest.mark.asyncio()
async def test_restart_applies_overrides(
    aiohttp_client, tmp_path: Path,
) -> None:
    """POST /autopilot/restart applies request body overrides."""
    project_id = "test_restart_overrides"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _create_project(storage_root, project_id)

    from aiohttp import web

    from manga_autopilot.services.autopilot import AutopilotController

    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: FakeExecutor()
    ctrl = AutopilotController()
    app["manga_autopilot_controller"] = ctrl
    register_all(app, storage_root=str(storage_root))

    client = await aiohttp_client(app)
    r = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/restart",
        json={"page_count": 4, "candidate_count": 2},
    )
    assert r.status == 202

    run = ctrl.runs.get(project_id)
    assert run is not None
    # Overrides should take precedence
    assert run.input.get("page_count") == 4
    assert run.input.get("candidate_count") == 2
    # Non-overridden values should be restored from project.json
    assert run.input.get("max_retries") == 0


@pytest.mark.asyncio()
async def test_cancelled_project_can_restart_and_complete(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Full E2E: cancel → restart → completed."""
    from aiohttp import web

    llm = FakeLLMProvider()
    executor = SlowFakeExecutor()
    app = web.Application()
    app["manga_llm_provider"] = llm
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: executor
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    # Step 1: Create the project via the project API
    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Restart Test", "title": "Restart"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]
    paths = ensure_project_paths(tmp_path, project_id)

    # Step 2: Start the autopilot
    start_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "A hero stands tall",
            "page_count": 1,
            "panels_per_page": 1,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # Step 3: Wait briefly then cancel
    await asyncio.sleep(0.1)
    cancel_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/cancel",
        json={"reason": "test cancellation"},
    )
    assert cancel_resp.status == 200

    # Step 4: Wait for the run to finish (cancelled)
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(
            f"/manga_autopilot/api/projects/{project_id}/autopilot/status",
        )
        status_data = await r.json()
        if status_data["state"] in ("CANCELLED", "FAILED"):
            break
        await asyncio.sleep(0.05)

    assert status_data["state"] == "CANCELLED"

    # Step 5: Cancel marker should exist
    assert paths.cancel_json.exists()

    # Step 6: Restart
    restart_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/restart",
        json={"reason": "restart after cancel"},
    )
    assert restart_resp.status == 202
    restart_data = await restart_resp.json()
    assert restart_data["restarted"] is True

    # Step 7: Cancel marker should be deleted
    assert not paths.cancel_json.exists()

    # Step 8: Wait for completion
    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(
            f"/manga_autopilot/api/projects/{project_id}/autopilot/status",
        )
        status_data = await r.json()
        if status_data["state"] in ("COMPLETED", "FAILED"):
            break
        await asyncio.sleep(0.05)

    # Step 9: Verify final state
    assert status_data["state"] == "COMPLETED"

    # Step 10: Verify generation_log.json exists
    gen_log_path = paths.generation_log_json
    assert gen_log_path.exists()
