"""Opt-in E2E smoke test against a live ComfyUI server.

This test is **skipped by default**.  To run it you must set three
environment variables:

    MANGA_AUTOPILOT_REAL_COMFY_E2E=1
    MANGA_AUTOPILOT_COMFY_BASE_URL=http://127.0.0.1:8188
    MANGA_AUTOPILOT_TEST_WORKFLOW_JSON=/path/to/workflow_api.json

The base URL can point to any reachable ComfyUI instance (localhost, LAN
PC, cloud GPU, etc.).  The workflow JSON must be a valid workflow payload
(as accepted by ``WorkflowRegistry.register``) with an ``api_graph`` and
appropriate ``bindings``.

Optional:

    MANGA_AUTOPILOT_REAL_COMFY_TIMEOUT=180   (seconds, default 180)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from manga_autopilot.routes import register_all
from manga_autopilot.services.comfy_client import ComfyClient
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.services.workflow_registry import WorkflowRegistry

# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------

_REAL_COMFY_E2E = os.environ.get("MANGA_AUTOPILOT_REAL_COMFY_E2E", "0").strip() in ("1", "true", "True", "yes")
_COMFY_BASE_URL = os.environ.get("MANGA_AUTOPILOT_COMFY_BASE_URL", "").strip()
_WORKFLOW_JSON_PATH = os.environ.get("MANGA_AUTOPILOT_TEST_WORKFLOW_JSON", "").strip()
_REAL_COMFY_TIMEOUT = int(os.environ.get("MANGA_AUTOPILOT_REAL_COMFY_TIMEOUT", "180"))

_skip_reason: str | None = None
if not _REAL_COMFY_E2E:
    _skip_reason = "MANGA_AUTOPILOT_REAL_COMFY_E2E is not set"
elif not _COMFY_BASE_URL:
    _skip_reason = "MANGA_AUTOPILOT_COMFY_BASE_URL is not set"
elif not _WORKFLOW_JSON_PATH:
    _skip_reason = "MANGA_AUTOPILOT_TEST_WORKFLOW_JSON is not set"

NeedsRealComfy = pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or "real ComfyUI not configured")


# ---------------------------------------------------------------------------
# FakeLLMProvider (same as other E2E tests)
# ---------------------------------------------------------------------------

def _parse_page_count(prompt: str) -> int:
    m = re.search(r"ページ数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


def _parse_panel_count(prompt: str) -> int:
    m = re.search(r"パネル数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


class FakeLLMProvider(LLMProvider):
    """LLM that returns canned plans for the full autopilot pipeline."""

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
                "width": 512,
                "height": 512,
            })
        return "{}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
async def real_comfy_e2e(aiohttp_client, tmp_path: Path):
    """E2E fixture wired to a real ComfyUI server.

    The fixture is only reachable when the ``NeedsRealComfy`` marker
    passes; otherwise the test is skipped before this fixture runs.
    """

    # Validate that the workflow JSON file exists (config error → fail).
    workflow_path = Path(_WORKFLOW_JSON_PATH)
    if not workflow_path.exists():
        pytest.fail(
            f"MANGA_AUTOPILOT_TEST_WORKFLOW_JSON points to a non-existent file: {workflow_path}"
        )

    workflow_payload = json.loads(workflow_path.read_text(encoding="utf-8"))

    # Build a real ComfyClient pointing at the live server.
    real_client = ComfyClient(base_url=_COMFY_BASE_URL, timeout_sec=_REAL_COMFY_TIMEOUT)

    # Connectivity check — if the server is unreachable, skip gracefully.
    try:
        await real_client.get_object_info()
    except Exception as exc:
        await real_client.close()
        pytest.skip(f"Real ComfyUI server is not reachable at {_COMFY_BASE_URL}: {exc}")

    # Register the workflow in a real WorkflowRegistry.
    registry = WorkflowRegistry.open(tmp_path / "registry")
    registry.register(workflow_payload)

    llm = FakeLLMProvider()

    app = web.Application()
    app["manga_llm_provider"] = llm
    app["manga_default_workflow_id"] = workflow_payload.get("workflow_id", "anime_t2i_default")
    app["manga_comfy_client"] = real_client
    app["manga_workflow_registry"] = registry
    # Deliberately NOT setting manga_panel_executor_factory

    register_all(app, storage_root=str(tmp_path))
    cli = await aiohttp_client(app)

    yield cli, tmp_path, llm, real_client, registry

    await real_client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _wait_for_completion(cli, project_id: str, timeout: float = 180.0) -> dict[str, Any]:
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
        await asyncio.sleep(0.5)
    pytest.fail(f"autopilot did not complete within {timeout}s; last state={last.get('state')}")
    return last


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@NeedsRealComfy
async def test_real_comfyui_executor_smoke_e2e(real_comfy_e2e) -> None:
    """1-page / 1-panel smoke test against a live ComfyUI server."""
    cli, tmp_path, llm, real_client, registry = real_comfy_e2e

    # 1. Create the project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "Real ComfyUI E2E", "title": "RealComfy"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "A hero standing on a cliff at sunset",
            "page_count": 1,
            "panels_per_page": 1,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=float(_REAL_COMFY_TIMEOUT))
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. Panel record exists with a real image.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 1
    rec = panels[0]
    assert rec["image_path"] is not None
    assert Path(rec["image_path"]).exists()
    assert rec["status"] == "generated"
    assert rec["page_number"] == 1

    # 5. Job exists and is completed.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 1
    job = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert job["status"] == "completed"

    # 6. Page PNG was rendered.
    page_path = project_root / "exports" / "pages" / "page_0001.png"
    assert page_path.exists()
    assert page_path.stat().st_size > 0

    # 7. Bubbles exist.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 1
    assert bubbles[0]["text"]

    # 8. Manifest is correct.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 1
    assert manifest["stats"]["panel_count"] == 1
    assert manifest["stats"]["generated_images"] == 1

    # 9. generation_log.json confirms COMPLETED.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 10. LLM was exercised.
    assert len(llm.calls) >= 5
