"""Resume E2E: fail mid-generation, then resume to complete missing panels only (issue #169).

Steps:
  1. Generate 4-page × 2-panel project with ``FailingAfterNExecutor`` that
     raises on the 4th call → pipeline transitions to FAILED_PANEL_GENERATION.
  2. Verify artefacts: panels.json has 8 records, some with image_path, some
     without; generation_log.json has FAILED state; jobs/ has completed + failed.
  3. Create a brand-new app instance over the same storage root with a
     succeeding executor (simulates restart).
  4. Call ``POST .../autopilot/start`` with the same input → idempotent
     ``generate_panels`` hook skips already-generated panels and generates
     only the missing ones.
  5. Verify final state: COMPLETED, all 8 panels generated, exports exist,
     manifest correct.
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
from manga_autopilot.services.generation_job import GenerationExecutorResult
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings  # noqa: I001

# --------------------------------------------------------- fakes
_PAGE_PANEL_DIALOGUES: dict[int, dict[int, str]] = {
    1: {1: "行くぞ", 2: "ここからだ"},
    2: {1: "負けない", 2: "進むしかない"},
    3: {1: "見えた", 2: "まだ終わらない"},
    4: {1: "決める", 2: "終わらせる"},
}

_PANEL_DIALOGUES: dict[int, str] = {
    1: "行くぞ",
    2: "任せる",
    3: "了解",
    4: "よし",
}


def _parse_page_count(prompt: str) -> int:
    m = re.search(r"ページ数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


def _parse_page_number(prompt: str) -> int:
    m = re.search(r'"?(?:pageNumber|page_number)"?\s*[：:]\s*(\d+)', prompt)
    return int(m.group(1)) if m else 1


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
            pc = _parse_page_count(prompt)
            pages = [
                {
                    "pageNumber": i + 1,
                    "summary": f"Page {i + 1} summary",
                    "emotionalGoal": "determined",
                    "visualGoal": "wide shot",
                    "panelCount": 2,
                }
                for i in range(pc)
            ]
            return json.dumps(
                {
                    "title": f"Sample {pc}-Page",
                    "logline": "A hero adventure.",
                    "genre": "fantasy",
                    "pages": pages,
                }
            )
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
        if (schema or {}).get("required") and "pages" in (schema or {}).get("required", []):
            pc = _parse_page_count(prompt)
            pages = [
                {
                    "pageNumber": i + 1,
                    "summary": f"Page {i + 1} summary",
                    "emotionalGoal": "determined",
                    "visualGoal": "wide shot",
                    "panelCount": 2,
                }
                for i in range(pc)
            ]
            return json.dumps({"pages": pages})
        if (schema or {}).get("required") and "panels" in (schema or {}).get("required", []):
            pc = _parse_panel_count(prompt)
            pn = _parse_page_number(prompt)
            panels = []
            for i in range(pc):
                page_dialogues = _PAGE_PANEL_DIALOGUES.get(pn, {})
                text = page_dialogues.get(i + 1, _PANEL_DIALOGUES.get(i + 1, "行くぞ"))
                panels.append(
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
                                "text": text,
                                "type": "speech",
                                "characterId": "char_hero",
                            }
                        ],
                    }
                )
            return json.dumps({"panels": panels})
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
        return "{}"


class FailingAfterNExecutor:
    """Executor that raises RuntimeError on the Nth submit call."""

    def __init__(self, fail_on_call: int) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_on_call = fail_on_call

    async def submit(self, *, prompt, workflow_id, seed, candidate_id):
        self.calls.append({"candidate_id": candidate_id, "seed": seed, "workflow_id": workflow_id})
        if len(self.calls) == self.fail_on_call:
            raise RuntimeError("intentional executor failure")
        image = Image.new("RGB", (prompt.width, prompt.height), (seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=candidate_id,
            prompt_id=f"prompt_{candidate_id}",
            image=image,
            workflow_id=workflow_id,
        )


class SucceedingExecutor:
    """Executor that always succeeds."""

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


# --------------------------------------------------------- helpers
async def _wait_for_terminal(cli, project_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await cli.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        assert resp.status == 200
        body = await resp.json()
        last = body
        state = body.get("state", "")
        if state == "COMPLETED" or state.startswith("FAILED") or state == "CANCELLED":
            return body
        await asyncio.sleep(0.05)
    pytest.fail(f"autopilot did not reach terminal state within {timeout}s; last state: {last.get('state')}")


# --------------------------------------------------------- test
async def test_failed_autopilot_can_resume_missing_panels_only(aiohttp_client, tmp_path: Path) -> None:
    """4p×2c → fail on panel 4 → resume → skip generated panels → complete."""

    # ===== STEP 1: First run — fails on 4th executor call =====
    fail_llm = FakeLLMProvider()
    fail_executor = FailingAfterNExecutor(fail_on_call=4)
    app = web.Application()
    app["manga_llm_provider"] = fail_llm
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda project_id: fail_executor
    register_all(app, storage_root=str(tmp_path))
    cli = await aiohttp_client(app)

    # Create project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "resume-e2e", "title": "Resume E2E", "page_count": 4},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Start autopilot — will fail partway through panel generation.
    input_payload = {
        "page_count": 4,
        "panels_per_page": 2,
        "candidate_count": 1,
        "max_retries": 0,
    }
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json=input_payload,
    )
    assert start_resp.status == 202

    # Wait for terminal state — should be FAILED.
    final = await _wait_for_terminal(cli, project_id, timeout=5.0)
    assert final["state"].startswith("FAILED"), f"expected FAILED state, got {final['state']}"

    project_root = tmp_path / "projects" / project_id

    # ===== STEP 2: Verify failure artefacts =====

    # panels.json has 8 records.
    panels_raw = json.loads((project_root / "panels.json").read_text(encoding="utf-8"))
    assert len(panels_raw) == 8

    # At least one panel has image_path (generated before failure).
    generated = [p for p in panels_raw if p.get("image_path") is not None]
    assert len(generated) >= 1, "at least one panel should be generated before failure"

    # At least one panel has image_path == None (not generated).
    pending = [p for p in panels_raw if p.get("image_path") is None]
    assert len(pending) >= 1, "at least one panel should be pending after failure"

    # Generated panel files exist on disk.
    for p in generated:
        assert Path(p["image_path"]).exists(), f"generated image missing: {p['image_path']}"

    # generation_log.json exists with FAILED state.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"].startswith("FAILED")

    # jobs/ has at least one completed job and one failed job.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.exists()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) >= 2
    job_statuses = set()
    for jf in job_files:
        job_data = json.loads(jf.read_text(encoding="utf-8"))
        job_statuses.add(job_data["status"])
    assert "completed" in job_statuses, "expected at least one completed job"
    assert "failed" in job_statuses, "expected at least one failed job"

    # Record the generated panel IDs and their image paths for later comparison.
    generated_panel_ids = {p["panel_id"] for p in generated}
    generated_image_paths = {p["panel_id"]: p["image_path"] for p in generated}

    # ===== STEP 3: Resume — new app, succeeding executor =====
    succeed_llm = FakeLLMProvider()
    succeed_executor = SucceedingExecutor()
    app2 = web.Application()
    app2["manga_llm_provider"] = succeed_llm
    app2["manga_default_workflow_id"] = "anime_t2i_default"
    app2["manga_panel_executor_factory"] = lambda project_id: succeed_executor
    register_all(app2, storage_root=str(tmp_path))
    cli2 = await aiohttp_client(app2)

    # Verify project still accessible via new app.
    get_resp = await cli2.get(f"/manga_autopilot/api/projects/{project_id}")
    assert get_resp.status == 200
    project_data = await get_resp.json()
    assert project_data["id"] == project_id

    # Start autopilot again with same input — idempotent generate_panels
    # will skip already-generated panels.
    start_resp2 = await cli2.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json=input_payload,
    )
    assert start_resp2.status == 202

    # Wait for COMPLETED.
    final2 = await _wait_for_terminal(cli2, project_id, timeout=5.0)
    assert final2["state"] == "COMPLETED", f"expected COMPLETED, got {final2['state']}"

    # ===== STEP 4: Verify resume result =====

    # panels.json: all 8 panels have image_path.
    panels_final = json.loads((project_root / "panels.json").read_text(encoding="utf-8"))
    assert len(panels_final) == 8
    for p in panels_final:
        assert p["image_path"] is not None, f"{p['panel_id']} missing image_path"
        assert Path(p["image_path"]).exists(), f"image missing for {p['panel_id']}"
        assert p["status"] == "generated", f"{p['panel_id']} status is {p['status']}"

    # Initially generated panels were NOT re-generated (same image_path).
    for pid in generated_panel_ids:
        final_record = next(p for p in panels_final if p["panel_id"] == pid)
        assert final_record["image_path"] == generated_image_paths[pid], (
            f"panel {pid} was re-generated instead of being skipped"
        )

    # Succeeding executor was called only for the missing panels.
    missing_count = len(pending)
    assert len(succeed_executor.calls) == missing_count, (
        f"expected {missing_count} executor calls, got {len(succeed_executor.calls)}"
    )

    # bubbles.json exists with bubbles for all 8 panels.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 8

    # exports/pages/ has all 4 page PNGs.
    pages_dir = project_root / "exports" / "pages"
    assert pages_dir.exists()
    page_pngs = sorted(p.name for p in pages_dir.glob("page_*.png"))
    assert len(page_pngs) == 4

    # exports/webtoon/ has files.
    webtoon_dir = project_root / "exports" / "webtoon"
    assert webtoon_dir.exists()
    assert any(p.suffix == ".png" for p in webtoon_dir.iterdir())

    # exports/pdf/manga.pdf exists.
    pdf_path = project_root / "exports" / "pdf" / "manga.pdf"
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0

    # manifest.json is complete.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 4
    assert manifest["stats"]["panel_count"] == 8
    assert manifest["stats"]["generated_images"] == 8
    assert manifest["exports"]["pages"]
    assert manifest["exports"]["webtoon"]
    assert manifest["exports"]["pdf"] is not None
    assert "manga.pdf" in manifest["exports"]["pdf"]

    # generation_log.json final state is COMPLETED.
    log_final = json.loads((project_root / "generation_log.json").read_text(encoding="utf-8"))
    assert log_final["state"] == "COMPLETED"
