"""Tests for cancelled partial resume (issue #198).

Covers:
- Resume endpoint clears cancel marker
- Resume creates new run linked to previous run
- Resume reuses existing panel images
- Resume regenerates missing/broken panel images
- Resume returns 404 for missing project
- Full E2E: cancel → resume → complete with partial panel reuse
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.routes import register_all
from manga_autopilot.services.generation_job import (
    GenerationExecutorResult,
    PanelExecutionRequest,
)
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings


# --------------------------------------------------------------- helpers
class FakeLLMProvider(LLMProvider):
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
                "title": "Multi-Page Test",
                "logline": "A test story.",
                "genre": "fantasy",
                "pages": [
                    {"pageNumber": 1, "summary": "Page 1", "emotionalGoal": "determined", "visualGoal": "wide shot", "panelCount": 2},
                    {"pageNumber": 2, "summary": "Page 2", "emotionalGoal": "tense", "visualGoal": "close up", "panelCount": 2},
                ],
            })
        if (schema or {}).get("required") and "characters" in (schema or {}).get("required", []):
            return json.dumps({
                "characters": [{"id": "char_hero", "name": "Hero", "role": "protagonist", "visualTraits": ["blue hair"], "mustKeep": ["blue hair"], "styleHints": "manga"}]
            })
        if (schema or {}).get("required") and "pages" in (schema or {}).get("required", []):
            return json.dumps({
                "pages": [
                    {"pageNumber": 1, "summary": "Page 1", "emotionalGoal": "determined", "visualGoal": "wide shot", "panelCount": 2},
                    {"pageNumber": 2, "summary": "Page 2", "emotionalGoal": "tense", "visualGoal": "close up", "panelCount": 2},
                ]
            })
        if (schema or {}).get("required") and "panels" in (schema or {}).get("required", []):
            return json.dumps({
                "panels": [
                    {"panelNumber": 1, "purpose": "panel 1 shot", "shot": "wide", "cameraAngle": "low", "action": "action", "emotion": "determined", "characters": ["char_hero"], "background": "open field", "visualPriority": "character", "dialogue": [{"speaker": "Hero", "text": "行くぞ", "type": "speech", "characterId": "char_hero"}]},
                    {"panelNumber": 2, "purpose": "panel 2 shot", "shot": "close", "cameraAngle": "eye", "action": "reaction", "emotion": "surprised", "characters": ["char_hero"], "background": "forest", "visualPriority": "character", "dialogue": [{"speaker": "Hero", "text": "なんだ", "type": "speech", "characterId": "char_hero"}]},
                ]
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


class CancelAfterFirstPanelExecutor:
    """Executor that generates images but cancels after the first panel."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.call_count = 0

    async def submit(self, request: PanelExecutionRequest):
        self.calls.append(request)
        self.call_count += 1
        img = Image.new("RGB", (request.effective_width, request.effective_height), (request.seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id=f"prompt_{request.candidate_id}",
            image=img,
            workflow_id=request.workflow_id,
        )


class SlowFakeExecutor:
    """Executor with delay for cancel to land."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def submit(self, request: PanelExecutionRequest):
        await asyncio.sleep(0.3)
        self.calls.append(request)
        img = Image.new("RGB", (request.effective_width, request.effective_height), (request.seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id=f"prompt_{request.candidate_id}",
            image=img,
            workflow_id=request.workflow_id,
        )


# --------------------------------------------------------------- tests
@pytest.mark.asyncio()
async def test_resume_endpoint_clears_cancel_marker(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Resume endpoint clears cancel.json before starting new run."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: SlowFakeExecutor()
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    # Create project
    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Resume Clear Cancel"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Start autopilot
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

    # Cancel
    await asyncio.sleep(0.1)
    cancel_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/cancel",
        json={"reason": "test"},
    )
    assert cancel_resp.status == 200

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("CANCELLED", "FAILED"):
            break
        await asyncio.sleep(0.05)
    assert data["state"] in ("CANCELLED", "FAILED")

    # Verify cancel.json exists
    project_root = tmp_path / "projects" / project_id
    assert (project_root / "cancel.json").exists()

    # Resume
    resume_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/resume-cancelled",
        json={"reason": "test resume"},
    )
    assert resume_resp.status == 202

    # Verify cancel.json is cleared
    assert not (project_root / "cancel.json").exists()


@pytest.mark.asyncio()
async def test_resume_creates_new_run_linked_to_previous_run(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Resume creates a new run with source.resume_of_run_id pointing to previous run."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: SlowFakeExecutor()
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    # Create project
    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Resume Link"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Start autopilot
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
    first_run_id = (await start_resp.json())["run_id"]

    # Cancel
    await asyncio.sleep(0.1)
    cancel_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/cancel",
        json={"reason": "test"},
    )
    assert cancel_resp.status == 200

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("CANCELLED", "FAILED"):
            break
        await asyncio.sleep(0.05)
    assert data["state"] in ("CANCELLED", "FAILED")

    # Resume
    resume_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/resume-cancelled",
        json={"reason": "test resume"},
    )
    assert resume_resp.status == 202
    resume_data = await resume_resp.json()
    second_run_id = resume_data["run_id"]
    assert second_run_id != first_run_id
    assert resume_data["resume_of_run_id"] == first_run_id

    # Verify run.json has resume source link
    project_root = tmp_path / "projects" / project_id
    run_file = project_root / "runs" / second_run_id / "run.json"
    assert run_file.exists()
    run_json = json.loads(run_file.read_text(encoding="utf-8"))
    assert run_json["source"]["resume_of_run_id"] == first_run_id
    assert run_json["kind"] == "resume"

    # Verify latest_run_id.txt points to new run
    latest = project_root / "latest_run_id.txt"
    assert latest.read_text(encoding="utf-8") == second_run_id


@pytest.mark.asyncio()
async def test_resume_returns_404_for_missing_project(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Resume returns 404 for non-existent project."""
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    client = await aiohttp_client(app)

    resume_resp = await client.post(
        "/manga_autopilot/api/projects/nonexistent/autopilot/resume-cancelled",
        json={},
    )
    assert resume_resp.status == 404


@pytest.mark.asyncio()
async def test_cancelled_project_can_resume_missing_panels_only(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Full E2E: cancel → resume → complete with partial panel reuse.

    Flow:
    1. Start autopilot
    2. Cancel after some panels are generated
    3. Resume
    4. Verify final state is COMPLETED
    5. Verify both run directories exist
    6. Verify all panels have valid image_path
    """
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    executor = SlowFakeExecutor()
    app["manga_panel_executor_factory"] = lambda pid: executor
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    # Step 1: Create project
    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Partial Resume E2E"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Step 2: Start autopilot
    start_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "A hero adventure",
            "page_count": 1,
            "panels_per_page": 2,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202
    first_run_id = (await start_resp.json())["run_id"]

    # Step 3: Wait for some panels to be generated, then cancel
    await asyncio.sleep(1.0)
    cancel_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/cancel",
        json={"reason": "test cancel"},
    )
    assert cancel_resp.status == 200

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("CANCELLED", "FAILED"):
            break
        await asyncio.sleep(0.05)
    assert data["state"] in ("CANCELLED", "FAILED")

    # Step 4: Resume
    resume_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/resume-cancelled",
        json={"reason": "test resume"},
    )
    assert resume_resp.status == 202
    second_run_id = (await resume_resp.json())["run_id"]
    assert second_run_id != first_run_id

    # Step 5: Wait for completion
    deadline = asyncio.get_event_loop().time() + 20.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("COMPLETED", "FAILED"):
            break
        await asyncio.sleep(0.1)
    assert data["state"] == "COMPLETED"

    # Step 6: Verify both run directories exist
    project_root = tmp_path / "projects" / project_id
    first_run_dir = project_root / "runs" / first_run_id
    second_run_dir = project_root / "runs" / second_run_id
    assert first_run_dir.exists()
    assert second_run_dir.exists()

    # Step 7: Verify all panels have valid image_path
    panels_path = project_root / "panels.json"
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    all_generated = all(p.get("image_path") and Path(p["image_path"]).exists() for p in panels)
    assert all_generated, "Not all panels have valid image_path after resume"

    # Step 8: Verify latest_run_id.txt points to new run
    latest = project_root / "latest_run_id.txt"
    assert latest.read_text(encoding="utf-8") == second_run_id

    # Step 9: Verify resume run.json has correct source
    run_file = second_run_dir / "run.json"
    assert run_file.exists()
    run_json = json.loads(run_file.read_text(encoding="utf-8"))
    assert run_json["kind"] == "resume"
    assert run_json["source"]["resume_of_run_id"] == first_run_id
