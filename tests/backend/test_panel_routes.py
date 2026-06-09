"""Tests for the panel generation HTTP API (spec section 21.6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.models.page import PanelPlan
from manga_autopilot.models.panel import (
    PanelLayout,
    PanelRecord,
    write_panel_records,
)
from manga_autopilot.routes import register_all
from manga_autopilot.services.generation_job import (
    GenerationExecutorResult,
    PanelExecutionRequest,
)


# --------------------------------------------------------- helpers
def _seed_panel(tmp_path: Path, project_id: str = "proj_test_001") -> PanelRecord:
    project_root = tmp_path / "projects" / project_id
    project_root.mkdir(parents=True, exist_ok=True)
    plan = PanelPlan(
        panel_number=1,
        purpose="hero shot",
        shot="medium",
        action="stands tall",
        emotion="determined",
    )
    layout = PanelLayout(panel_id="panel_001", x=0, y=0, width=512, height=512)
    record = PanelRecord(
        panel_id="panel_001",
        page_number=1,
        plan=plan,
        layout=layout,
        status="draft",
    )
    write_panel_records(project_root / "panels.json", [record])
    # Also need a project.json so PageEditorService doesn't choke.
    (project_root / "project.json").write_text(
        json.dumps({"id": project_id, "name": "test"}),
        encoding="utf-8",
    )
    return record


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def submit(self, request: PanelExecutionRequest):
        self.calls.append(request)
        image = Image.new("RGB", (request.effective_width, request.effective_height), (request.seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id=f"prompt_{request.candidate_id}",
            image=image,
            workflow_id=request.workflow_id,
        )


@pytest.fixture()
async def client(aiohttp_client, tmp_path: Path):
    executor = FakeExecutor()
    app = web.Application()
    app["manga_panel_executor_factory"] = lambda project_id: executor
    app["manga_panel_executor_for_test"] = executor  # for assertions
    register_all(app, storage_root=str(tmp_path))
    return await aiohttp_client(app), tmp_path, executor


# --------------------------------------------------------- generate
async def test_generate_panel_persists_image_and_job(client) -> None:
    cli, tmp_path, executor = client
    _seed_panel(tmp_path)
    resp = await cli.post(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001/generate",
        json={"width": 64, "height": 64, "candidate_count": 1, "max_retries": 0},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["job"]["status"] == "completed"
    assert body["job"]["panel_id"] == "panel_001"
    assert body["selected_image_path"] is not None

    # The image landed under assets/panels/.
    image_path = Path(body["selected_image_path"])
    assert image_path.exists()
    assert "panel_001" in image_path.name

    # The executor was called exactly once for a single candidate.
    assert len(executor.calls) == 1
    assert executor.calls[0].workflow_id == "anime_t2i_default"

    # The job was persisted to the jobs/ dir.
    project_root = tmp_path / "projects" / "proj_test_001"
    job_files = list((project_root / "jobs").iterdir())
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert payload["panel_id"] == "panel_001"
    assert payload["status"] == "completed"

    # The PanelRecord was updated with the new image_path.
    panels = json.loads((project_root / "panels.json").read_text(encoding="utf-8"))
    record = next(p for p in panels if p["panel_id"] == "panel_001")
    assert record["image_path"] == body["selected_image_path"]
    assert record["status"] == "generated"


async def test_generate_panel_missing_panel_returns_404(client) -> None:
    cli, tmp_path, _executor = client
    _seed_panel(tmp_path)
    resp = await cli.post(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_missing/generate",
        json={},
    )
    assert resp.status == 404


async def test_generate_panel_with_invalid_body_still_works(client) -> None:
    """A request with an empty body or non-JSON body must default to
    sensible prompt values, not crash."""

    cli, tmp_path, _executor = client
    _seed_panel(tmp_path)
    resp = await cli.post(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001/generate",
        data="not-json",
        headers={"content-type": "text/plain"},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["job"]["status"] in {"completed", "failed"}


# --------------------------------------------------------- regenerate / repair
async def test_regenerate_panel_reuses_overrides(client) -> None:
    cli, tmp_path, executor = client
    _seed_panel(tmp_path)
    resp = await cli.post(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001/regenerate",
        json={"positive": "epic battle", "seed": 999, "width": 64, "height": 64},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["job"]["status"] in {"completed", "failed"}
    # The executor was called with the requested seed.
    assert executor.calls and executor.calls[0].seed == 999


async def test_repair_panel_uses_more_retries(client) -> None:
    """``repair`` accepts a larger retry budget so the operator can re-render
    the same panel multiple times in a row."""

    cli, tmp_path, executor = client
    _seed_panel(tmp_path)
    resp = await cli.post(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001/repair",
        json={"max_retries": 0, "width": 64, "height": 64},
    )
    assert resp.status == 200  # 200 (not 201) signals "updated"
    body = await resp.json()
    assert body["job"]["status"] in {"completed", "failed"}


# --------------------------------------------------------- patch
async def test_patch_panel_updates_fields(client) -> None:
    cli, tmp_path, _executor = client
    _seed_panel(tmp_path)
    resp = await cli.patch(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001",
        json={"status": "approved", "notes": "looks good"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "approved"
    assert body["notes"] == "looks good"

    panels = json.loads((tmp_path / "projects" / "proj_test_001" / "panels.json").read_text("utf-8"))
    assert panels[0]["status"] == "approved"
    assert panels[0]["notes"] == "looks good"


async def test_patch_panel_rejects_invalid_status(client) -> None:
    cli, tmp_path, _executor = client
    _seed_panel(tmp_path)
    resp = await cli.patch(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001",
        json={"status": "not-a-status"},
    )
    assert resp.status == 400


async def test_patch_panel_clears_image_path(client) -> None:
    cli, tmp_path, _executor = client
    _seed_panel(tmp_path)
    resp = await cli.patch(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001",
        json={"image_path": None},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["image_path"] is None


# --------------------------------------------------------- get status
async def test_get_panel_status_returns_latest_job(client) -> None:
    cli, tmp_path, _executor = client
    _seed_panel(tmp_path)
    # First, generate so a job is persisted.
    gen_resp = await cli.post(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001/generate",
        json={"width": 64, "height": 64, "max_retries": 0},
    )
    assert gen_resp.status == 201

    status_resp = await cli.get(
        "/manga_autopilot/api/projects/proj_test_001/panels/panel_001"
    )
    assert status_resp.status == 200
    body = await status_resp.json()
    assert body["panel"]["panel_id"] == "panel_001"
    assert body["latest_job"] is not None
    assert body["latest_job"]["panel_id"] == "panel_001"
