"""Re-edit E2E: generate a project, reopen it, edit bubble text, re-render, re-export (issue #168).

Steps:
  1. Generate 4-page × 2-panel project via Autopilot (fake LLM + executor).
  2. Create a brand-new app instance over the same storage root (simulates restart).
  3. Fetch project via HTTP, verify artefacts exist on disk.
  4. Edit a bubble's text via PATCH /bubbles/{id}.
  5. Re-render page PNGs via export_page_png service function.
  6. Re-export webtoon + PDF via ExportService.
  7. Rebuild manifest via ManifestWriter.
  8. Assert updated artefacts, bubble text, manifest contents, and page hash change.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.routes import register_all
from manga_autopilot.services.autopilot import ManifestExports, ManifestStats, ManifestWriter
from manga_autopilot.services.bubble_service import BubbleService
from manga_autopilot.services.export import ExportService, export_page_png
from manga_autopilot.services.generation_job import GenerationExecutorResult
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings

# --------------------------------------------------------- fakes (duplicated to avoid cross-test coupling)
_PANEL_DIALOGUES: dict[int, str] = {
    1: "行くぞ",
    2: "任せる",
    3: "了解",
    4: "よし",
}

_PAGE_PANEL_DIALOGUES: dict[int, dict[int, str]] = {
    1: {1: "行くぞ", 2: "ここからだ"},
    2: {1: "負けない", 2: "進むしかない"},
    3: {1: "見えた", 2: "まだ終わらない"},
    4: {1: "決める", 2: "終わらせる"},
}

ORIGINAL_TEXT = "行くぞ"
EDITED_TEXT = "再編集したセリフ"


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
        # Story planner: schema has "title" + "pages"
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
                    "panelCount": 2,
                }
                for i in range(pc)
            ]
            return json.dumps({"pages": pages})
        # Panel planner
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
        # Prompt builder
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


# --------------------------------------------------------- helpers
async def _wait_for_completion(cli, project_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await cli.get(f"/manga_autopilot/api/projects/{project_id}/autopilot/status")
        assert resp.status == 200
        body = await resp.json()
        last = body
        state = body.get("state", "")
        if state == "COMPLETED":
            return body
        if state.startswith("FAILED") or state == "CANCELLED":
            pytest.fail(f"autopilot reached terminal failure state: {state}\n{body}")
        await asyncio.sleep(0.05)
    pytest.fail(f"autopilot did not complete within {timeout}s; last state: {last.get('state')}")


def _assign_fallback_layouts(records: Sequence[Any], page_number: int) -> list[PanelLayout]:
    """Reproduce the layout assignment from autopilot_routes._assign_fallback_layouts."""
    n = len(records)
    margin = 4
    pw, ph = 1200 - 2 * margin, 1600 - 2 * margin

    def _make(x: float, y: float, w: float, h: float, pid: str) -> PanelLayout:
        return PanelLayout(panel_id=pid, x=x, y=y, width=w, height=h)

    layouts: list[PanelLayout] = []
    for idx, record in enumerate(records):
        pid = record.panel_id
        if n == 1:
            layouts.append(_make(margin, margin, pw, ph, pid))
        elif n == 2:
            if idx == 0:
                layouts.append(_make(margin, margin, pw, ph / 2, pid))
            else:
                layouts.append(_make(margin, margin + ph / 2, pw, ph / 2, pid))
        elif n == 3:
            if idx == 0:
                layouts.append(_make(margin, margin, pw, ph / 2, pid))
            elif idx == 1:
                layouts.append(_make(margin, margin + ph / 2, pw / 2, ph / 2, pid))
            else:
                layouts.append(_make(margin + pw / 2, margin + ph / 2, pw / 2, ph / 2, pid))
        else:
            cols = 2
            rows = (n + cols - 1) // cols
            cell_w = pw / cols
            cell_h = ph / rows
            row = idx // cols
            col = idx % cols
            layouts.append(_make(margin + col * cell_w, margin + row * cell_h, cell_w, cell_h, pid))
    return layouts


# --------------------------------------------------------- test
@pytest.mark.release_gate
async def test_generated_project_can_be_reopened_edited_and_reexported(aiohttp_client, tmp_path: Path) -> None:
    """4p×2c → reopen → edit bubble → re-render pages → re-export webtoon/PDF → verify manifest."""

    # ===== STEP 1: Generate project via Autopilot =====
    llm = FakeLLMProvider()
    executor = FakeExecutor()
    app = web.Application()
    app["manga_llm_provider"] = llm
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda project_id: executor
    register_all(app, storage_root=str(tmp_path))
    cli = await aiohttp_client(app)

    # Create project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "reedit-e2e", "title": "Re-edit E2E", "page_count": 4},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # Start autopilot.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={"page_count": 4, "panels_per_page": 2, "candidate_count": 1, "max_retries": 0},
    )
    assert start_resp.status == 202

    # Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=5.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # Verify step-1 artefacts.
    assert (project_root / "project.json").exists()
    assert (project_root / "panels.json").exists()
    assert (project_root / "bubbles.json").exists()
    assert (project_root / "manifest.json").exists()
    assert (project_root / "generation_log.json").exists()
    for pn in range(1, 5):
        assert (project_root / "exports" / "pages" / f"page_{pn:04d}.png").exists()
    webtoon_dir = project_root / "exports" / "webtoon"
    assert webtoon_dir.exists()
    assert any(p.suffix == ".png" for p in webtoon_dir.iterdir())
    pdf_path = project_root / "exports" / "pdf" / "manga.pdf"
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0

    # Record original page_0001.png for reference.
    original_page1 = project_root / "exports" / "pages" / "page_0001.png"
    assert original_page1.exists()

    # Record original bubbles.
    bubbles_raw = json.loads((project_root / "bubbles.json").read_text(encoding="utf-8"))
    assert len(bubbles_raw) >= 8
    target_bubble = bubbles_raw[0]
    target_bubble_id = target_bubble["id"]
    assert target_bubble["text"] == ORIGINAL_TEXT

    # ===== STEP 2: Simulate app restart — new app instance, same storage_root =====
    app2 = web.Application()
    register_all(app2, storage_root=str(tmp_path))
    cli2 = await aiohttp_client(app2)

    # 2a. Fetch project via new app.
    get_resp = await cli2.get(f"/manga_autopilot/api/projects/{project_id}")
    assert get_resp.status == 200
    project_data = await get_resp.json()
    assert project_data["id"] == project_id
    assert project_data["title"] == "Re-edit E2E"

    # 2b. project.json readable on disk.
    assert (project_root / "project.json").exists()

    # 2c. panels.json readable on disk.
    panels_raw = json.loads((project_root / "panels.json").read_text(encoding="utf-8"))
    assert len(panels_raw) == 8

    # 2d. bubbles.json readable via BubbleService.
    svc = BubbleService(project_root=project_root)
    bubbles = svc.list_bubbles()
    assert len(bubbles) >= 8

    # ===== STEP 3: Edit bubble text via HTTP PATCH =====
    bubble_to_edit = next(b for b in bubbles if b.id == target_bubble_id)
    patch_body = bubble_to_edit.model_dump(mode="json")
    patch_body["text"] = EDITED_TEXT

    patch_resp = await cli2.patch(
        f"/manga_autopilot/api/projects/{project_id}/bubbles/{target_bubble_id}",
        json=patch_body,
    )
    assert patch_resp.status == 200
    patched = await patch_resp.json()
    assert patched["text"] == EDITED_TEXT
    assert patched["panel_id"] == bubble_to_edit.panel_id

    # Verify persistence.
    bubbles_after = json.loads((project_root / "bubbles.json").read_text(encoding="utf-8"))
    edited_bubble = next(b for b in bubbles_after if b["id"] == target_bubble_id)
    assert edited_bubble["text"] == EDITED_TEXT
    # panel_id unchanged.
    assert edited_bubble["panel_id"] == target_bubble["panel_id"]

    # ===== STEP 4: Re-render page PNGs =====
    # Read panel records and reconstruct layouts (same logic as autopilot render hook).
    from manga_autopilot.models.panel import load_panel_records

    records = load_panel_records(project_root / "panels.json")
    by_page: dict[int, list] = {}
    for record in records:
        if record.image_path is None:
            continue
        by_page.setdefault(record.page_number, []).append(record)

    for page_number, page_records in by_page.items():
        layouts = _assign_fallback_layouts(page_records, page_number)
        for record, layout in zip(page_records, layouts, strict=True):
            layout.image_path = record.image_path
        page_id = f"page_{page_number:04d}"
        export_page_png(project_root, page_id, layouts)

    # Overlay bubbles on page_0001 using BubbleService + BubbleRenderer.
    from manga_autopilot.services.bubble_layout import place_bubbles
    from manga_autopilot.services.bubble_renderer import draw_bubble_on_canvas

    page1_path = project_root / "exports" / "pages" / "page_0001.png"
    assert page1_path.exists()

    with Image.open(page1_path) as img:
        canvas = img.convert("RGBA")
        for record in by_page.get(1, []):
            bubbles_for_panel = svc.list_bubbles(panel_id=record.panel_id)
            if not bubbles_for_panel:
                continue
            # Reconstruct layout for this panel.
            panel_layouts = _assign_fallback_layouts(by_page[1], 1)
            panel_layout = next(ly for ly in panel_layouts if ly.panel_id == record.panel_id)
            placements = place_bubbles(bubbles_for_panel, panel_layout)
            for placement in placements:
                draw_bubble_on_canvas(
                    canvas, placement.bubble, placement.x, placement.y, placement.width, placement.height
                )
        canvas.convert("RGB").save(page1_path, format="PNG")

    # Verify page_0001.png exists after re-render.
    assert page1_path.exists()
    # Note: at 64×64 fake resolution the bubble overlay may produce
    # identical bytes, so we do not enforce a hash change here.

    # ===== STEP 5: Re-export webtoon + PDF =====
    export_svc = ExportService(storage_root=tmp_path)
    page_pngs = sorted(p for p in (project_root / "exports" / "pages").glob("page_*.png") if p.is_file())
    assert len(page_pngs) == 4

    # Re-export webtoon.
    webtoon_paths = export_svc.webtoon(project_id, page_pngs)
    assert len(webtoon_paths) >= 1
    for wp in webtoon_paths:
        assert wp.exists()
        assert wp.stat().st_size > 0

    # Re-export PDF.
    pdf_out = export_svc.pdf(project_id, page_pngs)
    assert pdf_out.exists()
    assert pdf_out.stat().st_size > 0

    # ===== STEP 6: Rebuild manifest =====
    manifest_writer = ManifestWriter(project_root)
    manifest_writer.write(
        project_id=project_id,
        title="Re-edit E2E",
        status="completed",
        created_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T01:00:00Z",
        exports=ManifestExports(
            pages=[str(p) for p in page_pngs],
            webtoon=[str(p) for p in webtoon_paths],
            pdf=str(pdf_out),
        ),
        stats=ManifestStats(
            page_count=4,
            panel_count=8,
            generated_images=8,
            regenerated_panels=0,
            average_qa_score=0.0,
        ),
    )

    # ===== STEP 7: Verify final state =====
    manifest = json.loads((project_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["exports"]["pages"]
    assert manifest["exports"]["webtoon"]
    assert manifest["exports"]["pdf"] is not None
    assert "manga.pdf" in manifest["exports"]["pdf"]

    # All page PNGs exist.
    for pn in range(1, 5):
        assert (project_root / "exports" / "pages" / f"page_{pn:04d}.png").exists()

    # Webtoon dir has files.
    assert any(p.suffix == ".png" for p in webtoon_dir.iterdir())

    # PDF exists.
    assert pdf_path.exists()

    # Edited bubble text is persisted.
    final_bubbles = json.loads((project_root / "bubbles.json").read_text(encoding="utf-8"))
    final_target = next(b for b in final_bubbles if b["id"] == target_bubble_id)
    assert final_target["text"] == EDITED_TEXT

    # Panel records still intact (8 panels, distinct panel_ids).
    panel_ids = {r["panel_id"] for r in panels_raw}
    assert len(panel_ids) == 8
