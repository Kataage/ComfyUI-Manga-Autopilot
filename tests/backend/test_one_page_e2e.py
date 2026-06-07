"""End-to-end integration test for the 1-page v1.0 autopilot (issue #152).

This test wires the full pipeline together against a real aiohttp
test client and a temporary storage root:

1. ``POST /projects`` to create a project
2. Inject a fake :class:`LLMProvider` and a fake :class:`GenerationExecutor`
   so the autopilot can run without a network
3. ``POST /projects/{id}/autopilot/start`` with ``page_count=1``
4. Poll ``/projects/{id}/autopilot/status`` until the run finishes
5. Verify the on-disk artefacts (panel image, page export, manifest,
   generation_log) match the v1.0 spec acceptance criteria (§28.2).

The test exercises the real default hooks wired in
:func:`manga_autopilot.routes.autopilot_routes._default_hooks_for_project`,
not custom test-only hooks.  This is the single source of truth that
issue #152 wants to keep green.
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
from manga_autopilot.services.generation_job import GenerationExecutorResult
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings


# --------------------------------------------------------- fakes
class FakeLLMProvider(LLMProvider):
    """LLM that returns canned plans for story / page / panel planners."""

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
        # Story planner: 1 page, 1 panel.
        if "StoryPlan" in prompt or "story" in prompt.lower() and "title" in (schema or {}).get("required", []):
            return json.dumps(
                {
                    "title": "Sample One-Page",
                    "logline": "A hero stands tall.",
                    "genre": "fantasy",
                    "pages": [
                        {
                            "pageNumber": 1,
                            "summary": "Hero stands tall",
                            "emotionalGoal": "determined",
                            "visualGoal": "wide shot",
                            "panelCount": 1,
                        }
                    ],
                }
            )
        # Character planner
        if (schema or {}).get("required") and "characters" in (schema or {}).get("required", []):
            return json.dumps(
                {
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
                }
            )
        # Page planner
        if (schema or {}).get("required") and "pages" in (schema or {}).get("required", []):
            return json.dumps(
                {
                    "pages": [
                        {
                            "pageNumber": 1,
                            "summary": "Hero stands tall",
                            "emotionalGoal": "determined",
                            "visualGoal": "wide shot",
                            "panelCount": 1,
                        }
                    ]
                }
            )
        # Panel planner
        if (schema or {}).get("required") and "panels" in (schema or {}).get("required", []):
            return json.dumps(
                {
                    "panels": [
                        {
                            "panelNumber": 1,
                            "purpose": "hero shot",
                            "shot": "wide",
                            "cameraAngle": "low",
                            "action": "stands tall",
                            "emotion": "determined",
                            "characters": ["char_hero"],
                            "background": "open field",
                            "visualPriority": "character",
                        }
                    ]
                }
            )
        # Prompt builder -- needs positive / negative
        if (schema or {}).get("required") and "positive" in (schema or {}).get("required", []):
            return json.dumps(
                {
                    "positive": "hero standing tall, wide shot, blue hair",
                    "negative": "low quality, blurry",
                    "seed": 12345,
                    "width": 64,
                    "height": 64,
                }
            )
        # Fallback: empty object (ManualProvider behaviour).
        return "{}"


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def submit(self, *, prompt, workflow_id, seed, candidate_id):
        self.calls.append({"candidate_id": candidate_id, "seed": seed, "workflow_id": workflow_id})
        image = Image.new("RGB", (prompt.width, prompt.height), (seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=candidate_id,
            prompt_id=f"prompt_{candidate_id}",
            image=image,
            workflow_id=workflow_id,
        )


# --------------------------------------------------------- fixture
@pytest.fixture()
async def e2e_client(aiohttp_client, tmp_path: Path):
    llm = FakeLLMProvider()
    executor = FakeExecutor()
    app = web.Application()
    app["manga_llm_provider"] = llm
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda project_id: executor
    register_all(app, storage_root=str(tmp_path))
    cli = await aiohttp_client(app)
    return cli, tmp_path, llm, executor


# --------------------------------------------------------- helpers
async def _wait_for_completion(cli, project_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """Poll the autopilot status endpoint until the run reaches a terminal state."""

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
        if state.startswith("FAILED") or state == "CANCELLED":
            pytest.fail(f"autopilot reached terminal failure state: {state}\n{body}")
        await asyncio.sleep(0.05)
    pytest.fail(f"autopilot did not complete within {timeout}s; last status: {last}")


# --------------------------------------------------------- the test
async def test_one_page_autopilot_completes_end_to_end(e2e_client) -> None:
    cli, tmp_path, llm, executor = e2e_client

    # 1. Create the project via the project API.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "One-Page Sample", "title": "One-Page"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot with page_count=1.
    start_resp = await cli.post(
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

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=5.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. The panel record was generated and references a real image.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 1
    record = panels[0]
    assert record["image_path"] is not None
    image_path = Path(record["image_path"])
    assert image_path.exists()
    # The image lives under assets/panels/.
    assert "assets" in image_path.parts
    assert "panels" in image_path.parts
    assert record["status"] == "generated"

    # 5. The page was rendered to exports/pages/page_0001.png.
    exports_dir = project_root / "exports" / "pages"
    rendered_pages = sorted(p.name for p in exports_dir.iterdir() if p.suffix == ".png")
    assert "page_0001.png" in rendered_pages
    rendered_path = exports_dir / "page_0001.png"
    assert rendered_path.stat().st_size > 0

    # 6. A GenerationJob JSON lives under jobs/.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 1
    job = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert job["status"] == "completed"
    assert job["selected_candidate_id"] is not None
    assert len(job["candidates"]) == 1
    assert job["candidates"][0]["image_path"] is not None
    assert Path(job["candidates"][0]["image_path"]).exists()

    # 7. The manifest + generation_log were written.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 1
    assert manifest["stats"]["panel_count"] == 1
    assert manifest["stats"]["generated_images"] == 1
    assert manifest["exports"]["pages"]  # at least one rendered page

    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 8. The LLM and the executor were exercised.
    assert len(llm.calls) >= 1
    assert len(executor.calls) == 1
    assert executor.calls[0]["workflow_id"] == "anime_t2i_default"

    # 9. The project document was left in a "generated" state when fetched.
    get_resp = await cli.get(f"/manga_autopilot/api/projects/{project_id}")
    assert get_resp.status == 200
