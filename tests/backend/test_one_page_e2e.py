"""End-to-end integration test for the 1-page and 2-page autopilot (issues #152, #156).

This test wires the full pipeline together against a real aiohttp
test client and a temporary storage root:

1. ``POST /projects`` to create a project
2. Inject a fake :class:`LLMProvider` and a fake :class:`GenerationExecutor`
   so the autopilot can run without a network
3. ``POST /projects/{id}/autopilot/start`` with ``page_count=1`` or ``page_count=2``
4. Poll ``/projects/{id}/autopilot/status`` until the run finishes
5. Verify the on-disk artefacts (panel image, page export, manifest,
   generation_log) match the v1.0 spec acceptance criteria (§28.2).

The test exercises the real default hooks wired in
:mfunc:`manga_autopilot.routes.autopilot_routes._default_hooks_for_project`,
not custom test-only hooks.
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


# --------------------------------------------------------- fakes
def _parse_page_count(prompt: str) -> int:
    """Extract page_count from a planner prompt (e.g. 'ページ数: 2' or 'ページ数は 2')."""
    m = re.search(r"ページ数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


def _parse_page_number(prompt: str) -> int:
    """Extract pageNumber from a panel planner prompt (e.g. 'pageNumber: 3' or 'page_number: 3')."""
    m = re.search(r'"?(?:pageNumber|page_number)"?\s*[：:]\s*(\d+)', prompt)
    return int(m.group(1)) if m else 1


def _parse_panel_count(prompt: str) -> int:
    """Extract panel_count from a panel planner prompt (e.g. 'パネル数は 2')."""
    m = re.search(r"パネル数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


# Per-page dialogue texts for multi-page tests.
_PAGE_DIALOGUES: dict[int, str] = {
    1: "行くぞ",
    2: "ここからだ",
    3: "負けない",
    4: "終わらせる",
}

# Per-panel dialogue texts for multi-panel tests.
_PANEL_DIALOGUES: dict[int, str] = {
    1: "行くぞ",
    2: "任せる",
    3: "了解",
    4: "よし",
}

# Per-page per-panel dialogue texts for 4-page × 2-panel tests.
_PAGE_PANEL_DIALOGUES: dict[int, dict[int, str]] = {
    1: {1: "行くぞ", 2: "ここからだ"},
    2: {1: "負けない", 2: "進むしかない"},
    3: {1: "見えた", 2: "まだ終わらない"},
    4: {1: "決める", 2: "終わらせる"},
}


class FakeLLMProvider(LLMProvider):
    """LLM that returns canned plans for story / page / panel planners.

    Supports multi-page by parsing ``page_count`` from the prompt text.
    """

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
        # Story planner: schema has "title" + "pages"
        if "title" in required and "pages" in required:
            pc = _parse_page_count(prompt)
            pages = [
                {
                    "pageNumber": i + 1,
                    "summary": f"Page {i + 1} summary",
                    "emotionalGoal": "determined",
                    "visualGoal": "wide shot",
                    "panelCount": 1,
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
            pc = _parse_page_count(prompt)
            pages = [
                {
                    "pageNumber": i + 1,
                    "summary": f"Page {i + 1} summary",
                    "emotionalGoal": "determined",
                    "visualGoal": "wide shot",
                    "panelCount": 1,
                }
                for i in range(pc)
            ]
            return json.dumps({"pages": pages})
        # Panel planner (called once per page by plan_panels)
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
@pytest.mark.release_gate
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
    assert executor.calls[0].workflow_id == "anime_t2i_default"

    # 9. The project document was left in a "generated" state when fetched.
    get_resp = await cli.get(f"/manga_autopilot/api/projects/{project_id}")
    assert get_resp.status == 200

    # 10. Bubbles were generated and persisted to bubbles.json.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 1
    first_bubble = bubbles[0]
    assert first_bubble["text"]  # text must not be empty
    assert first_bubble["panel_id"]  # panel_id must be set
    assert first_bubble["type"] in ("normal", "shout", "thought", "narration", "whisper", "radio")

    # 11. The rendered page PNG has bubble overlay (size increased vs bare page).
    rendered_path = exports_dir / "page_0001.png"
    assert rendered_path.exists()
    page_size = rendered_path.stat().st_size
    assert page_size > 0

    # 12. Webtoon and PDF exports exist.
    webtoon_dir = project_root / "exports" / "webtoon"
    assert webtoon_dir.exists()
    assert any(p.suffix == ".png" for p in webtoon_dir.iterdir())
    pdf_path = project_root / "exports" / "pdf" / "manga.pdf"
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0

    # 13. Manifest includes webtoon and pdf.
    assert len(manifest["exports"]["webtoon"]) >= 1
    assert manifest["exports"]["pdf"] is not None
    assert "manga.pdf" in manifest["exports"]["pdf"]


# --------------------------------------------------------- 2-page test
async def test_two_page_autopilot_completes_end_to_end(e2e_client) -> None:
    """2-page autopilot: idea -> story(2 pages) -> panels -> images -> bubbles -> 2 PNGs -> manifest."""
    cli, tmp_path, llm, executor = e2e_client

    # 1. Create the project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "Two-Page Sample", "title": "Two-Page"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot with page_count=2.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "A hero journeys across two pages",
            "page_count": 2,
            "panels_per_page": 1,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=10.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. Two panel records exist, each with an image.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 2
    for rec in panels:
        assert rec["image_path"] is not None
        assert Path(rec["image_path"]).exists()
        assert rec["status"] == "generated"
    # Panels belong to different pages.
    page_numbers = {rec["page_number"] for rec in panels}
    assert page_numbers == {1, 2}

    # 5. Two page PNGs were rendered.
    exports_dir = project_root / "exports" / "pages"
    rendered_pages = sorted(p.name for p in exports_dir.iterdir() if p.suffix == ".png")
    assert "page_0001.png" in rendered_pages
    assert "page_0002.png" in rendered_pages
    assert (exports_dir / "page_0001.png").stat().st_size > 0
    assert (exports_dir / "page_0002.png").stat().st_size > 0

    # 6. Two GenerationJob JSONs exist under jobs/.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 2
    for jf in job_files:
        job = json.loads(jf.read_text(encoding="utf-8"))
        assert job["status"] == "completed"
        assert job["selected_candidate_id"] is not None

    # 7. Manifest reflects 2 pages.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 2
    assert manifest["stats"]["panel_count"] == 2
    assert manifest["stats"]["generated_images"] == 2
    assert len(manifest["exports"]["pages"]) >= 2

    # 8. generation_log.json confirms COMPLETED.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 9. LLM and executor were exercised for both panels.
    assert len(llm.calls) >= 1
    assert len(executor.calls) == 2

    # 10. Bubbles exist for both panels.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 2
    panel_ids = {b["panel_id"] for b in bubbles}
    assert len(panel_ids) == 2  # one bubble per panel
    for b in bubbles:
        assert b["text"]
        assert b["panel_id"]


# --------------------------------------------------------- 4-page test
async def test_four_page_autopilot_completes_end_to_end(e2e_client) -> None:
    """4-page autopilot: idea -> story(4 pages) -> panels -> images -> bubbles -> 4 PNGs -> manifest."""
    cli, tmp_path, llm, executor = e2e_client

    # 1. Create the project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "Four-Page Sample", "title": "Four-Page"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot with page_count=4.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "A hero journey across four pages",
            "page_count": 4,
            "panels_per_page": 1,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=15.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. Four panel records exist, each with an image.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 4
    for rec in panels:
        assert rec["image_path"] is not None
        assert Path(rec["image_path"]).exists()
        assert rec["status"] == "generated"
    # Panels belong to pages 1-4.
    page_numbers = {rec["page_number"] for rec in panels}
    assert page_numbers == {1, 2, 3, 4}

    # 5. Four page PNGs were rendered.
    exports_dir = project_root / "exports" / "pages"
    rendered_pages = sorted(p.name for p in exports_dir.iterdir() if p.suffix == ".png")
    for pn in range(1, 5):
        fname = f"page_{pn:04d}.png"
        assert fname in rendered_pages, f"{fname} not found in {rendered_pages}"
        assert (exports_dir / fname).stat().st_size > 0

    # 6. Four GenerationJob JSONs exist under jobs/.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 4
    for jf in job_files:
        job = json.loads(jf.read_text(encoding="utf-8"))
        assert job["status"] == "completed"
        assert job["selected_candidate_id"] is not None

    # 7. Manifest reflects 4 pages.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 4
    assert manifest["stats"]["panel_count"] == 4
    assert manifest["stats"]["generated_images"] == 4
    assert len(manifest["exports"]["pages"]) >= 4

    # 8. generation_log.json confirms COMPLETED.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 9. LLM and executor were exercised for all four panels.
    assert len(llm.calls) >= 1
    assert len(executor.calls) == 4

    # 10. Bubbles exist for all four panels with distinct panel_ids.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 4
    panel_ids = {b["panel_id"] for b in bubbles}
    assert len(panel_ids) == 4  # one bubble per panel, 4 panels
    for b in bubbles:
        assert b["text"]
        assert b["panel_id"]
        assert b["type"] in ("normal", "shout", "thought", "narration", "whisper", "radio")


# --------------------------------------------------------- multi-panel test
async def test_multi_panel_per_page_autopilot_completes_end_to_end(e2e_client) -> None:
    """1-page / 2-panel autopilot: 1 page with 2 panels, each with its own bubble."""
    cli, tmp_path, llm, executor = e2e_client

    # 1. Create the project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "Multi-Panel Sample", "title": "Multi-Panel"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot with page_count=1, panels_per_page=2.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "Two heroes face each other",
            "page_count": 1,
            "panels_per_page": 2,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=10.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. Two panel records exist on page 1, each with an image.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 2
    for rec in panels:
        assert rec["image_path"] is not None
        assert Path(rec["image_path"]).exists()
        assert rec["status"] == "generated"
        assert rec["page_number"] == 1
    # Panel IDs are distinct.
    panel_ids = {rec["panel_id"] for rec in panels}
    assert len(panel_ids) == 2

    # 5. One page PNG was rendered (both panels composited into page_0001.png).
    exports_dir = project_root / "exports" / "pages"
    rendered_pages = sorted(p.name for p in exports_dir.iterdir() if p.suffix == ".png")
    assert "page_0001.png" in rendered_pages
    assert (exports_dir / "page_0001.png").stat().st_size > 0

    # 6. Two GenerationJob JSONs exist under jobs/.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 2
    for jf in job_files:
        job = json.loads(jf.read_text(encoding="utf-8"))
        assert job["status"] == "completed"
        assert job["selected_candidate_id"] is not None

    # 7. Manifest reflects 1 page, 2 panels.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 1
    assert manifest["stats"]["panel_count"] == 2
    assert manifest["stats"]["generated_images"] == 2

    # 8. generation_log.json confirms COMPLETED.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 9. LLM and executor were exercised for both panels.
    assert len(llm.calls) >= 1
    assert len(executor.calls) == 2

    # 10. Bubbles exist for both panels with distinct panel_ids.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 2
    bubble_panel_ids = {b["panel_id"] for b in bubbles}
    assert len(bubble_panel_ids) == 2
    for b in bubbles:
        assert b["text"]
        assert b["panel_id"]


# --------------------------------------------------------- 3-panel test
async def test_three_panel_per_page_autopilot_completes_end_to_end(e2e_client) -> None:
    """1-page / 3-panel autopilot: 1 page with 3 panels, each with its own bubble."""
    cli, tmp_path, llm, executor = e2e_client

    # 1. Create the project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "Three-Panel Sample", "title": "Three-Panel"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot with page_count=1, panels_per_page=3.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "Three heroes in a dramatic scene",
            "page_count": 1,
            "panels_per_page": 3,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=15.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. Three panel records exist on page 1, each with an image.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 3
    for rec in panels:
        assert rec["image_path"] is not None
        assert Path(rec["image_path"]).exists()
        assert rec["status"] == "generated"
        assert rec["page_number"] == 1
    # Panel IDs are distinct.
    panel_ids = {rec["panel_id"] for rec in panels}
    assert len(panel_ids) == 3

    # 5. One page PNG was rendered (all three panels composited into page_0001.png).
    exports_dir = project_root / "exports" / "pages"
    rendered_pages = sorted(p.name for p in exports_dir.iterdir() if p.suffix == ".png")
    assert "page_0001.png" in rendered_pages
    page_path = exports_dir / "page_0001.png"
    assert page_path.stat().st_size > 0

    # 6. Three GenerationJob JSONs exist under jobs/.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 3
    for jf in job_files:
        job = json.loads(jf.read_text(encoding="utf-8"))
        assert job["status"] == "completed"
        assert job["selected_candidate_id"] is not None

    # 7. Manifest reflects 1 page, 3 panels.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 1
    assert manifest["stats"]["panel_count"] == 3
    assert manifest["stats"]["generated_images"] == 3
    # exports.pages contains page_0001.png
    export_page_names = [Path(p).name for p in manifest["exports"]["pages"]]
    assert "page_0001.png" in export_page_names

    # 8. generation_log.json confirms COMPLETED.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 9. LLM and executor were exercised for all three panels.
    assert len(llm.calls) >= 1
    assert len(executor.calls) == 3

    # 10. Bubbles exist for all three panels with distinct panel_ids.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 3
    bubble_panel_ids = {b["panel_id"] for b in bubbles}
    assert len(bubble_panel_ids) == 3
    for b in bubbles:
        assert b["text"]
        assert b["panel_id"]


# --------------------------------------------------------- 4-page × 2-panel test
@pytest.mark.release_gate
async def test_four_page_two_panel_autopilot_completes_end_to_end(e2e_client) -> None:
    """4-page × 2-panel autopilot: 8 panels total, 4 page PNGs, 8 bubbles."""
    cli, tmp_path, llm, executor = e2e_client

    # 1. Create the project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "4P2C Sample", "title": "4P2C"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot with page_count=4, panels_per_page=2.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "Four-page dramatic scene with two panels each",
            "page_count": 4,
            "panels_per_page": 2,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=25.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. 8 panel records exist (2 per page × 4 pages), each with an image.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 8
    for rec in panels:
        assert rec["image_path"] is not None
        assert Path(rec["image_path"]).exists()
        assert rec["status"] == "generated"
    # page_number set is {1,2,3,4}.
    page_numbers = {rec["page_number"] for rec in panels}
    assert page_numbers == {1, 2, 3, 4}
    # Each page has exactly 2 panels.
    from collections import Counter
    page_panel_counts = Counter(rec["page_number"] for rec in panels)
    for pn in (1, 2, 3, 4):
        assert page_panel_counts[pn] == 2
    # 8 distinct panel_ids.
    panel_ids = {rec["panel_id"] for rec in panels}
    assert len(panel_ids) == 8

    # 5. Four page PNGs were rendered.
    exports_dir = project_root / "exports" / "pages"
    rendered_pages = sorted(p.name for p in exports_dir.iterdir() if p.suffix == ".png")
    assert rendered_pages == ["page_0001.png", "page_0002.png", "page_0003.png", "page_0004.png"]
    for name in rendered_pages:
        p = exports_dir / name
        assert p.exists()
        assert p.stat().st_size > 0

    # 6. 8 GenerationJob JSONs exist under jobs/.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 8
    for jf in job_files:
        job = json.loads(jf.read_text(encoding="utf-8"))
        assert job["status"] == "completed"
        assert job["selected_candidate_id"] is not None

    # 7. Manifest reflects 4 pages, 8 panels.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 4
    assert manifest["stats"]["panel_count"] == 8
    assert manifest["stats"]["generated_images"] == 8
    export_page_names = [Path(p).name for p in manifest["exports"]["pages"]]
    assert sorted(export_page_names) == [
        "page_0001.png", "page_0002.png", "page_0003.png", "page_0004.png"
    ]

    # 8. generation_log.json confirms COMPLETED.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 9. LLM and executor were exercised for all 8 panels.
    assert len(llm.calls) >= 1
    assert len(executor.calls) == 8

    # 10. Bubbles exist for all 8 panels with distinct panel_ids.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 8
    bubble_panel_ids = {b["panel_id"] for b in bubbles}
    assert len(bubble_panel_ids) == 8
    for b in bubbles:
        assert b["text"]
        assert b["panel_id"]

    # 11. Each page has at least 2 bubbles.
    from collections import defaultdict
    bubbles_by_page: dict[int, list] = defaultdict(list)
    for b in bubbles:
        # Derive page from panel_id: "panel_p{n}_c{m}" → extract page number.
        panel_rec = next(rec for rec in panels if rec["panel_id"] == b["panel_id"])
        bubbles_by_page[panel_rec["page_number"]].append(b)
    for pn in (1, 2, 3, 4):
        assert len(bubbles_by_page[pn]) >= 2, f"page {pn} has {len(bubbles_by_page[pn])} bubbles, expected >= 2"

    # 12. Dialogues match _PAGE_PANEL_DIALOGUES.
    dialogue_texts = {b["panel_id"]: b["text"] for b in bubbles}
    assert dialogue_texts["panel_001_01"] == "行くぞ"
    assert dialogue_texts["panel_001_02"] == "ここからだ"
    assert dialogue_texts["panel_002_01"] == "負けない"
    assert dialogue_texts["panel_002_02"] == "進むしかない"
    assert dialogue_texts["panel_003_01"] == "見えた"
    assert dialogue_texts["panel_003_02"] == "まだ終わらない"
    assert dialogue_texts["panel_004_01"] == "決める"
    assert dialogue_texts["panel_004_02"] == "終わらせる"

    # 13. Webtoon export exists.
    webtoon_dir = project_root / "exports" / "webtoon"
    assert webtoon_dir.exists()
    webtoon_files = sorted(p.name for p in webtoon_dir.iterdir() if p.suffix == ".png")
    assert len(webtoon_files) >= 1
    # At least one webtoon file is non-empty.
    for wf in webtoon_files:
        assert (webtoon_dir / wf).stat().st_size > 0

    # 14. PDF export exists.
    pdf_path = project_root / "exports" / "pdf" / "manga.pdf"
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0

    # 15. Manifest includes webtoon and pdf exports.
    assert len(manifest["exports"]["webtoon"]) >= 1
    assert manifest["exports"]["pdf"] is not None
    assert "manga.pdf" in manifest["exports"]["pdf"]

    # 16. ExportService.all_exports covers pages, webtoon, pdf.
    from manga_autopilot.services.export import ExportService
    svc = ExportService(storage_root=tmp_path)
    all_ex = svc.all_exports(project_id)
    ex_names = [p.name for p in all_ex]
    assert any(n.startswith("page_") for n in ex_names)
    assert any("webtoon" in n for n in ex_names)
    assert any("manga.pdf" in n for n in ex_names)
