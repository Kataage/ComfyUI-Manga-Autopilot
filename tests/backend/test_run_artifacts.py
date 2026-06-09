"""Tests for per-run artifact directories (issue #194).

Covers:
- Mirror service (mirror_latest_artifacts_to_run, read_run_artifacts_summary, inject_artifacts_root_to_manifest)
- Completed run artifacts mirrored to run directory
- Restart creates separate run artifact directories
- Cancelled run mirrors generation log and partial outputs
- Project root latest outputs remain available
- run.json contains artifact summary
- manifest.json contains artifacts_root
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
from manga_autopilot.services.generation_job import (
    GenerationExecutorResult,
    PanelExecutionRequest,
)
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.services.run_artifacts import (
    inject_artifacts_root_to_manifest,
    mirror_latest_artifacts_to_run,
    read_run_artifacts_summary,
)


# --------------------------------------------------------------- helpers
def _parse_panel_count(prompt: str) -> int:
    m = re.search(r"パネル数[：:は]\s*(\d+)", prompt)
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


# --------------------------------------------------------------- mirror service unit tests
def test_mirror_copies_json_files(tmp_path: Path) -> None:
    """mirror copies existing JSON files from project root to run dir."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    run_id = "run_20260609_120000_aabbccdd"

    # Create source files
    (project_root / "generation_log.json").write_text('{"run_id": "x"}')
    (project_root / "panels.json").write_text('[]')
    (project_root / "bubbles.json").write_text('[]')

    mirrored = mirror_latest_artifacts_to_run(project_root, run_id)

    assert "generation_log.json" in mirrored
    assert "panels.json" in mirrored
    assert "bubbles.json" in mirrored
    assert (project_root / "runs" / run_id / "generation_log.json").exists()
    assert (project_root / "runs" / run_id / "panels.json").exists()


def test_mirror_skips_missing_files(tmp_path: Path) -> None:
    """mirror silently skips files that do not exist on project root."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    run_id = "run_20260609_120000_aabbccdd"

    mirrored = mirror_latest_artifacts_to_run(project_root, run_id)
    assert mirrored == {}


def test_mirror_copies_directories(tmp_path: Path) -> None:
    """mirror copies jobs/assets/exports directories."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    run_id = "run_20260609_120000_aabbccdd"

    # Create source directories with files
    jobs_dir = project_root / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "job_001.json").write_text('{}')

    assets_dir = project_root / "assets" / "panels"
    assets_dir.mkdir(parents=True)
    (assets_dir / "panel_001.png").write_bytes(b'\x89PNG')

    exports_dir = project_root / "exports" / "pages"
    exports_dir.mkdir(parents=True)
    (exports_dir / "page_0001.png").write_bytes(b'\x89PNG')

    mirrored = mirror_latest_artifacts_to_run(project_root, run_id)

    assert "jobs" in mirrored
    assert "assets" in mirrored
    assert "exports" in mirrored
    assert (project_root / "runs" / run_id / "jobs" / "job_001.json").exists()
    assert (project_root / "runs" / run_id / "assets" / "panels" / "panel_001.png").exists()
    assert (project_root / "runs" / run_id / "exports" / "pages" / "page_0001.png").exists()


def test_mirror_does_not_overwrite_run_json(tmp_path: Path) -> None:
    """mirror does not overwrite an existing run.json."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    run_id = "run_20260609_120000_aabbccdd"

    # Create existing run.json in run dir
    run_dir = project_root / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text('{"run_id": "existing"}')

    # Also create generation_log.json in project root
    (project_root / "generation_log.json").write_text('{"run_id": "x"}')

    mirror_latest_artifacts_to_run(project_root, run_id)

    # run.json should be unchanged
    run_json = json.loads((run_dir / "run.json").read_text())
    assert run_json["run_id"] == "existing"


def test_read_run_artifacts_summary(tmp_path: Path) -> None:
    """read_run_artifacts_summary returns correct paths for existing artifacts."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    run_id = "run_20260609_120000_aabbccdd"

    run_dir = project_root / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "generation_log.json").write_text('{}')
    (run_dir / "manifest.json").write_text('{}')
    (run_dir / "jobs").mkdir()

    summary = read_run_artifacts_summary(project_root, run_id)

    assert summary["generation_log.json"] == f"runs/{run_id}/generation_log.json"
    assert summary["manifest.json"] == f"runs/{run_id}/manifest.json"
    assert summary["jobs"] == f"runs/{run_id}/jobs"
    assert summary["panels.json"] is None  # does not exist


def test_inject_artifacts_root(tmp_path: Path) -> None:
    """inject_artifacts_root_to_manifest adds artifacts_root to manifest."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    run_id = "run_20260609_120000_aabbccdd"

    manifest = {"project_id": "proj_test", "run_id": run_id, "exports": {}}
    (project_root / "manifest.json").write_text(json.dumps(manifest))

    result = inject_artifacts_root_to_manifest(project_root, run_id)
    assert result is True

    updated = json.loads((project_root / "manifest.json").read_text())
    assert updated["artifacts_root"] == f"runs/{run_id}"


def test_inject_artifacts_root_missing_manifest(tmp_path: Path) -> None:
    """inject_artifacts_root_to_manifest returns False when manifest missing."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    result = inject_artifacts_root_to_manifest(project_root, "run_test")
    assert result is False


# --------------------------------------------------------------- E2E: completed run
@pytest.mark.asyncio()
async def test_completed_run_artifacts_are_mirrored_to_run_directory(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Completed run artifacts are mirrored under runs/{run_id}/."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: FakeExecutor()
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Mirror Test", "title": "MirrorTest"},
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
    run_id = (await start_resp.json())["run_id"]

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("COMPLETED", "FAILED"):
            break
        await asyncio.sleep(0.05)

    assert data["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id
    run_dir = project_root / "runs" / run_id

    # generation_log.json mirrored
    assert (run_dir / "generation_log.json").exists()
    gen_log = json.loads((run_dir / "generation_log.json").read_text())
    assert gen_log.get("run_id") == run_id

    # panels.json mirrored
    assert (run_dir / "panels.json").exists()

    # jobs directory mirrored (may be empty)
    assert (run_dir / "jobs").is_dir()

    # run.json has artifacts summary
    run_json = json.loads((run_dir / "run.json").read_text())
    assert "artifacts" in run_json
    assert run_json["artifacts"]["generation_log.json"] == f"runs/{run_id}/generation_log.json"

    # manifest.json has artifacts_root
    manifest = json.loads((project_root / "manifest.json").read_text())
    assert manifest.get("artifacts_root") == f"runs/{run_id}"


# --------------------------------------------------------------- E2E: project root preserved
@pytest.mark.asyncio()
async def test_project_root_latest_outputs_remain_available(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Project root latest outputs remain available after mirror."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: FakeExecutor()
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Root Preserved", "title": "RootPreserved"},
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
    assert (project_root / "generation_log.json").exists()
    assert (project_root / "panels.json").exists()
    assert (project_root / "manifest.json").exists()


# --------------------------------------------------------------- E2E: restart creates separate dirs
@pytest.mark.asyncio()
async def test_restart_creates_separate_run_artifact_directories(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Restart creates separate run artifact directories for old and new runs."""
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
        json={"name": "Restart Artifacts", "title": "RestartArtifacts"},
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
    second_run_id = (await restart_resp.json())["run_id"]
    assert second_run_id != first_run_id

    project_root = tmp_path / "projects" / project_id
    first_run_dir = project_root / "runs" / first_run_id

    # Wait for _finalize to complete mirroring asynchronously
    deadline2 = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline2:
        if (first_run_dir / "generation_log.json").exists():
            break
        await asyncio.sleep(0.05)

    # Both run directories should exist
    second_run_dir = project_root / "runs" / second_run_id
    assert first_run_dir.exists()
    assert second_run_dir.exists()

    # Both should have run.json
    assert (first_run_dir / "run.json").exists()
    assert (second_run_dir / "run.json").exists()

    # First run's generation_log should have been mirrored
    assert (first_run_dir / "generation_log.json").exists()

    # latest_run_id.txt points to second run
    latest = project_root / "latest_run_id.txt"
    assert latest.read_text(encoding="utf-8") == second_run_id

    # Second run has restart source link
    second_run_json = json.loads((second_run_dir / "run.json").read_text())
    assert second_run_json["source"]["restart_of_run_id"] == first_run_id


# --------------------------------------------------------------- E2E: cancelled run mirrors partial
@pytest.mark.asyncio()
async def test_cancelled_run_mirrors_generation_log_and_partial_outputs(
    aiohttp_client, tmp_path: Path,
) -> None:
    """Cancelled run mirrors generation_log.json and partial outputs."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    executor = SlowFakeExecutor()
    app["manga_panel_executor_factory"] = lambda pid: executor
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Cancel Mirror", "title": "CancelMirror"},
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
    run_id = (await start_resp.json())["run_id"]

    await asyncio.sleep(0.1)
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
    assert data["state"] == "CANCELLED"

    project_root = tmp_path / "projects" / project_id
    run_dir = project_root / "runs" / run_id

    # Wait for _finalize to complete mirroring asynchronously
    deadline2 = asyncio.get_event_loop().time() + 3.0
    while asyncio.get_event_loop().time() < deadline2:
        if (run_dir / "generation_log.json").exists():
            break
        await asyncio.sleep(0.05)

    # generation_log.json should exist in run dir (mirrored on cancel)
    assert (run_dir / "generation_log.json").exists()

    # run.json should exist with artifacts summary
    run_json = json.loads((run_dir / "run.json").read_text())
    assert run_json["status"] in ("CANCELLED", "FAILED_PANEL_GENERATION")
    assert "artifacts" in run_json


# --------------------------------------------------------------- run.json artifact summary E2E
@pytest.mark.asyncio()
async def test_run_json_contains_artifact_summary(
    aiohttp_client, tmp_path: Path,
) -> None:
    """run.json contains artifacts summary after completed run."""
    app = web.Application()
    app["manga_llm_provider"] = FakeLLMProvider()
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda pid: FakeExecutor()
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    create_resp = await client.post(
        "/manga_autopilot/api/projects",
        json={"name": "Artifact Summary", "title": "ArtifactSummary"},
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
    run_id = (await start_resp.json())["run_id"]

    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        data = await r.json()
        if data["state"] in ("COMPLETED", "FAILED"):
            break
        await asyncio.sleep(0.05)

    assert data["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id
    run_json = json.loads((project_root / "runs" / run_id / "run.json").read_text())

    assert "artifacts" in run_json
    assert run_json["artifacts"]["generation_log.json"] is not None
    assert run_json["artifacts"]["manifest.json"] is not None
    assert run_json["artifacts"]["panels.json"] is not None
