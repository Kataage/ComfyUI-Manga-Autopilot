"""Tests for run history foundation (issue #192).

Covers:
- run_id generation
- run metadata persistence (runs/{run_id}/run.json + latest_run_id.txt)
- run_id in generation_log.json
- run_id in manifest.json
- run_id in remote worker payload metadata
- restart source link (restart_of_run_id)
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.routes import register_all
from manga_autopilot.services.autopilot import (
    AutopilotRun,
    AutopilotStateMachine,
    generate_run_id,
    save_run_metadata,
)
from manga_autopilot.services.generation_job import (
    GenerationExecutorResult,
    PanelExecutionRequest,
)
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings


# --------------------------------------------------------------- helpers
def _parse_panel_count(prompt: str) -> int:
    m = re.search(r"パネル数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


def _parse_page_number(prompt: str) -> int:
    m = re.search(r'"?(?:pageNumber|page_number)"?\s*[：:]\s*(\d+)', prompt)
    return int(m.group(1)) if m else 1


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
        self.calls.append(request)
        img = Image.new("RGB", (request.effective_width, request.effective_height), (request.seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id=f"prompt_{request.candidate_id}",
            image=img,
            workflow_id=request.workflow_id,
        )


class SlowFakeExecutor(FakeExecutor):
    async def submit(self, request: PanelExecutionRequest):
        await asyncio.sleep(0.5)
        return await super().submit(request)


# --------------------------------------------------------------- run_id generation
def test_generate_run_id_format() -> None:
    """run_id matches run_YYYYMMDD_HHMMSS_<8hex> format."""
    rid = generate_run_id()
    assert re.match(r"^run_\d{8}_\d{6}_[0-9a-f]{8}$", rid), f"bad format: {rid}"


def test_generate_run_id_unique() -> None:
    """Two calls produce different run_ids."""
    ids = {generate_run_id() for _ in range(20)}
    assert len(ids) == 20


# --------------------------------------------------------------- save_run_metadata
def test_save_run_metadata_creates_files(tmp_path: Path) -> None:
    """save_run_metadata creates runs/{run_id}/run.json and latest_run_id.txt."""
    project_root = tmp_path / "projects" / "proj_test"
    project_root.mkdir(parents=True)

    machine = AutopilotStateMachine(project_id="proj_test")
    run = AutopilotRun(project_id="proj_test", machine=machine, run_id="run_20260609_120000_aabbccdd")
    run.input = {"page_count": 1}

    save_run_metadata(project_root, run)

    run_file = project_root / "runs" / "run_20260609_120000_aabbccdd" / "run.json"
    assert run_file.exists()
    data = json.loads(run_file.read_text(encoding="utf-8"))
    assert data["run_id"] == "run_20260609_120000_aabbccdd"
    assert data["project_id"] == "proj_test"
    assert data["status"] == "PROJECT_CREATED"
    assert data["input"]["page_count"] == 1
    assert data["source"]["restart_of_run_id"] is None

    latest = project_root / "latest_run_id.txt"
    assert latest.exists()
    assert latest.read_text(encoding="utf-8") == "run_20260609_120000_aabbccdd"


def test_save_run_metadata_updates_status(tmp_path: Path) -> None:
    """save_run_metadata updates status fields on completion."""
    project_root = tmp_path / "projects" / "proj_test"
    project_root.mkdir(parents=True)

    machine = AutopilotStateMachine(project_id="proj_test")
    run = AutopilotRun(project_id="proj_test", machine=machine, run_id="run_20260609_120000_aabbccdd")
    run.input = {"page_count": 1}

    save_run_metadata(project_root, run)

    # Simulate full pipeline completion
    for reason in [
        "validate_input", "plan_story", "define_characters",
        "generate_character_sheets", "plan_pages", "plan_panels",
        "build_prompts", "validate_workflow", "generate_panels",
        "qa_panels", "lettering", "render_pages", "export", "finalize",
    ]:
        machine.advance(reason)
    run.finish()
    save_run_metadata(project_root, run)

    run_file = project_root / "runs" / "run_20260609_120000_aabbccdd" / "run.json"
    data = json.loads(run_file.read_text(encoding="utf-8"))
    assert data["status"] == "COMPLETED"
    assert data["completed_at"] is not None


def test_save_run_metadata_cancellation(tmp_path: Path) -> None:
    """save_run_metadata records cancelled_at when status is CANCELLED."""
    from manga_autopilot.services.autopilot import AutopilotState

    project_root = tmp_path / "projects" / "proj_test"
    project_root.mkdir(parents=True)

    machine = AutopilotStateMachine(project_id="proj_test")
    run = AutopilotRun(project_id="proj_test", machine=machine, run_id="run_cancel_test")
    run.input = {}

    machine.jump(AutopilotState.CANCELLED)
    run.finish()
    save_run_metadata(project_root, run)

    run_file = project_root / "runs" / "run_cancel_test" / "run.json"
    data = json.loads(run_file.read_text(encoding="utf-8"))
    assert data["status"] == "CANCELLED"
    assert data["cancelled_at"] is not None


def test_save_run_metadata_restart_source(tmp_path: Path) -> None:
    """save_run_metadata records restart_of_run_id when set."""
    project_root = tmp_path / "projects" / "proj_test"
    project_root.mkdir(parents=True)

    machine = AutopilotStateMachine(project_id="proj_test")
    run = AutopilotRun(project_id="proj_test", machine=machine, run_id="run_new")
    run.source["restart_of_run_id"] = "run_previous"
    run.input = {}

    save_run_metadata(project_root, run)

    run_file = project_root / "runs" / "run_new" / "run.json"
    data = json.loads(run_file.read_text(encoding="utf-8"))
    assert data["source"]["restart_of_run_id"] == "run_previous"
    assert data["kind"] == "restart"


# --------------------------------------------------------------- run_id in generation_log.json
@pytest.mark.asyncio()
async def test_generation_log_contains_run_id(
    aiohttp_client, tmp_path: Path,
) -> None:
    """generation_log.json includes run_id after completion."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: FakeExecutor()
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Run ID Test", "title": "RunID"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

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

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("COMPLETED", "FAILED"):
            break
        await asyncio.sleep(0.05)

    assert data["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id
    gen_log_path = project_root / "generation_log.json"
    assert gen_log_path.exists()
    gen_log = json.loads(gen_log_path.read_text(encoding="utf-8"))
    assert "run_id" in gen_log
    assert re.match(r"^run_", gen_log["run_id"])

    # run_id in status response
    assert "run_id" in data
    assert data["run_id"] == gen_log["run_id"]


# --------------------------------------------------------------- run_id in manifest.json
@pytest.mark.asyncio()
async def test_manifest_contains_run_id_after_completion(
    aiohttp_client, tmp_path: Path,
) -> None:
    """manifest.json includes run_id after completion."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: FakeExecutor()
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Manifest RunID", "title": "ManifestRunID"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

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

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("COMPLETED", "FAILED"):
            break
        await asyncio.sleep(0.05)

    assert data["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "run_id" in manifest
    assert re.match(r"^run_", manifest["run_id"])


# --------------------------------------------------------------- run_id in remote payload
@pytest.mark.asyncio()
async def test_remote_executor_payload_contains_run_id_metadata(
    aiohttp_client, tmp_path: Path,
) -> None:
    """PanelExecutionRequest.metadata includes run_id."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    executor = FakeExecutor()
    app["manga_panel_executor_factory"] = lambda pid: executor
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Remote RunID", "title": "RemoteRunID"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

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

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("COMPLETED", "FAILED"):
            break
        await asyncio.sleep(0.05)

    assert data["state"] == "COMPLETED"
    assert len(executor.calls) >= 1
    for call in executor.calls:
        assert "run_id" in call.metadata
        assert re.match(r"^run_", call.metadata["run_id"])


# --------------------------------------------------------------- restart creates new run with source link
@pytest.mark.asyncio()
async def test_cancelled_restart_creates_new_run_and_links_previous_run(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Restart creates a new run with source.restart_of_run_id pointing to previous run."""
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
        json={"name": "Restart Link", "title": "RestartLink"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Step 2: Start autopilot
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

    # Step 3: Cancel
    await asyncio.sleep(0.1)
    cancel_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/cancel",
        json={"reason": "test"},
    )
    assert cancel_resp.status == 200

    # Wait for cancel to complete
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("CANCELLED", "FAILED"):
            break
        await asyncio.sleep(0.05)
    assert data["state"] == "CANCELLED"

    # Step 4: Restart
    restart_resp = await client.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/restart",
        json={"reason": "test restart"},
    )
    assert restart_resp.status == 202
    restart_data = await restart_resp.json()
    second_run_id = restart_data["run_id"]
    assert second_run_id != first_run_id

    # Step 5: Verify latest_run_id.txt points to new run
    project_root = tmp_path / "projects" / project_id
    latest = project_root / "latest_run_id.txt"
    assert latest.exists()
    assert latest.read_text(encoding="utf-8") == second_run_id

    # Step 6: Verify restart run.json has source link
    run_file = project_root / "runs" / second_run_id / "run.json"
    assert run_file.exists()
    run_data = json.loads(run_file.read_text(encoding="utf-8"))
    assert run_data["source"]["restart_of_run_id"] == first_run_id
    assert run_data["kind"] == "restart"
