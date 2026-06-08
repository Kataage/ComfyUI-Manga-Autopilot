"""Bundle import E2E: generate, ZIP export, import to different storage, re-edit, re-export (issue #170).

Steps:
  1. Generate 4-page × 2-panel project via Autopilot (fake LLM + executor).
  2. ZIP-export the project via ExportService.zip().
  3. Import ZIP to a different storage_root via ExportService.import_zip().
  4. Create a new app instance over the imported storage, fetch project.
  5. Edit a bubble's text via PATCH /bubbles/{id}.
  6. Re-render page PNGs, overlay edited bubble text.
  7. Re-export webtoon + PDF.
  8. Rebuild manifest and verify all artefacts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import zipfile
from collections.abc import Sequence
from datetime import datetime, timezone
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

# --------------------------------------------------------- fakes
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
EDITED_TEXT = "ZIPインポート後に編集したセリフ"


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
                    "width": 1200,
                    "height": 1600,
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


def _render_all_pages(
    project_root: Path,
    svc: BubbleService,
) -> None:
    """Render all page PNGs from panel images + bubble overlay."""
    from manga_autopilot.models.panel import load_panel_records
    from manga_autopilot.services.bubble_layout import place_bubbles
    from manga_autopilot.services.bubble_renderer import draw_bubble_on_canvas

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

    # Overlay bubbles on each page.
    for page_number, page_records in by_page.items():
        page_path = project_root / "exports" / "pages" / f"page_{page_number:04d}.png"
        if not page_path.exists():
            continue
        with Image.open(page_path) as img:
            canvas = img.convert("RGBA")
            for record in page_records:
                bubbles_for_panel = svc.list_bubbles(panel_id=record.panel_id)
                if not bubbles_for_panel:
                    continue
                panel_layouts = _assign_fallback_layouts(page_records, page_number)
                panel_layout = next(ly for ly in panel_layouts if ly.panel_id == record.panel_id)
                placements = place_bubbles(bubbles_for_panel, panel_layout)
                for placement in placements:
                    draw_bubble_on_canvas(
                        canvas,
                        placement.bubble,
                        placement.x,
                        placement.y,
                        placement.width,
                        placement.height,
                    )
            canvas.convert("RGB").save(page_path, format="PNG")


# --------------------------------------------------------- test
@pytest.mark.release_gate
async def test_generated_project_bundle_can_be_imported_edited_and_reexported(
    aiohttp_client, tmp_path: Path
) -> None:
    """4p×2c → ZIP export → import to different storage → edit bubble → re-render → re-export → verify."""

    # ===== STEP 1: Generate project in source storage =====
    source_storage = tmp_path / "source"
    source_storage.mkdir()

    llm = FakeLLMProvider()
    executor = FakeExecutor()
    app = web.Application()
    app["manga_llm_provider"] = llm
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_panel_executor_factory"] = lambda project_id: executor
    register_all(app, storage_root=str(source_storage))
    cli = await aiohttp_client(app)

    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "bundle-e2e", "title": "Bundle E2E", "page_count": 4},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={"page_count": 4, "panels_per_page": 2, "candidate_count": 1, "max_retries": 0},
    )
    assert start_resp.status == 202

    final = await _wait_for_completion(cli, project_id, timeout=5.0)
    assert final["state"] == "COMPLETED"

    source_root = source_storage / "projects" / project_id

    # Verify Step 1 artefacts.
    assert (source_root / "project.json").exists()
    assert (source_root / "panels.json").exists()
    assert (source_root / "bubbles.json").exists()
    assert (source_root / "manifest.json").exists()
    assert (source_root / "generation_log.json").exists()
    for pn in range(1, 5):
        assert (source_root / "exports" / "pages" / f"page_{pn:04d}.png").exists()
    webtoon_dir = source_root / "exports" / "webtoon"
    assert webtoon_dir.exists()
    assert any(p.suffix == ".png" for p in webtoon_dir.iterdir())
    pdf_path = source_root / "exports" / "pdf" / "manga.pdf"
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0
    jobs_dir = source_root / "jobs"
    assert jobs_dir.exists()
    assert len(list(jobs_dir.iterdir())) >= 8

    # Record original page_0001.png hash for change detection.
    original_page1 = source_root / "exports" / "pages" / "page_0001.png"
    page1_hash_before = hashlib.md5(original_page1.read_bytes()).hexdigest()

    # ===== STEP 2: ZIP export =====
    source_svc = ExportService(storage_root=source_storage)
    bundle_path = tmp_path / "bundle" / f"{project_id}.zip"
    source_svc.zip(project_id, bundle_path)

    assert bundle_path.exists()
    assert bundle_path.stat().st_size > 0

    # Verify ZIP contents.
    with zipfile.ZipFile(bundle_path) as zf:
        zip_names = set(zf.namelist())
    assert "project.json" in zip_names
    assert "panels.json" in zip_names
    assert "bubbles.json" in zip_names
    assert "manifest.json" in zip_names
    assert "generation_log.json" in zip_names
    assert "exports/pages/page_0001.png" in zip_names
    assert any(n.startswith("exports/webtoon/") for n in zip_names)
    assert "exports/pdf/manga.pdf" in zip_names

    # ===== STEP 3: Import ZIP to different storage =====
    imported_storage = tmp_path / "imported"
    imported_storage.mkdir()

    imported_svc = ExportService(storage_root=imported_storage)
    imported_root = imported_svc.import_zip(bundle_path, project_id=project_id)

    assert imported_root.exists()
    assert (imported_root / "project.json").exists()
    assert (imported_root / "panels.json").exists()
    assert (imported_root / "bubbles.json").exists()
    assert (imported_root / "manifest.json").exists()
    assert (imported_root / "generation_log.json").exists()

    # Verify imported panels.json has 8 records with image_path.
    imported_panels = json.loads((imported_root / "panels.json").read_text(encoding="utf-8"))
    assert len(imported_panels) == 8
    for p in imported_panels:
        assert p["image_path"] is not None

    # Verify imported bubbles.json has ≥8 entries.
    imported_bubbles_raw = json.loads((imported_root / "bubbles.json").read_text(encoding="utf-8"))
    assert len(imported_bubbles_raw) >= 8

    # Verify imported manifest.
    imported_manifest = json.loads((imported_root / "manifest.json").read_text(encoding="utf-8"))
    assert imported_manifest["exports"]["pages"]
    assert imported_manifest["exports"]["webtoon"]
    assert imported_manifest["exports"]["pdf"] is not None

    # Verify imported generation_log.json.
    imported_log = json.loads((imported_root / "generation_log.json").read_text(encoding="utf-8"))
    assert imported_log["state"] == "COMPLETED"
    assert imported_log["project_id"] == project_id

    # ===== STEP 4: New app instance over imported storage =====
    app2 = web.Application()
    register_all(app2, storage_root=str(imported_storage))
    cli2 = await aiohttp_client(app2)

    get_resp = await cli2.get(f"/manga_autopilot/api/projects/{project_id}")
    assert get_resp.status == 200
    project_data = await get_resp.json()
    assert project_data["id"] == project_id
    assert project_data["title"] == "Bundle E2E"

    # ===== STEP 5: Edit bubble text via PATCH =====
    svc = BubbleService(project_root=imported_root)
    bubbles = svc.list_bubbles()
    assert len(bubbles) >= 8

    target_bubble = bubbles[0]
    target_bubble_id = target_bubble.id
    original_panel_id = target_bubble.panel_id

    patch_body = target_bubble.model_dump(mode="json")
    patch_body["text"] = EDITED_TEXT

    patch_resp = await cli2.patch(
        f"/manga_autopilot/api/projects/{project_id}/bubbles/{target_bubble_id}",
        json=patch_body,
    )
    assert patch_resp.status == 200
    patched = await patch_resp.json()
    assert patched["text"] == EDITED_TEXT
    assert patched["panel_id"] == original_panel_id

    # Verify persistence in bubbles.json on disk.
    bubbles_after = json.loads((imported_root / "bubbles.json").read_text(encoding="utf-8"))
    edited_bubble = next(b for b in bubbles_after if b["id"] == target_bubble_id)
    assert edited_bubble["text"] == EDITED_TEXT
    assert edited_bubble["panel_id"] == original_panel_id

    # ===== STEP 6: Re-render page PNGs + overlay bubbles =====
    _render_all_pages(imported_root, svc)

    for pn in range(1, 5):
        assert (imported_root / "exports" / "pages" / f"page_{pn:04d}.png").exists()

    # Verify page_0001.png hash changed (re-rendered with edited bubble text).
    page1_reimported = imported_root / "exports" / "pages" / "page_0001.png"
    page1_hash_after = hashlib.md5(page1_reimported.read_bytes()).hexdigest()
    assert page1_hash_before != page1_hash_after, (
        "page_0001.png should change after re-render with edited bubble text"
    )

    # ===== STEP 7: Re-export webtoon + PDF =====
    imported_svc2 = ExportService(storage_root=imported_storage)
    page_pngs = sorted(
        p for p in (imported_root / "exports" / "pages").glob("page_*.png") if p.is_file()
    )
    assert len(page_pngs) == 4

    webtoon_paths = imported_svc2.webtoon(project_id, page_pngs)
    assert len(webtoon_paths) >= 1
    for wp in webtoon_paths:
        assert wp.exists()
        assert wp.stat().st_size > 0

    pdf_out = imported_svc2.pdf(project_id, page_pngs)
    assert pdf_out.exists()
    assert pdf_out.stat().st_size > 0

    # ===== STEP 8: Rebuild manifest =====
    records = json.loads((imported_root / "panels.json").read_text(encoding="utf-8"))
    generated_images = sum(1 for r in records if r.get("image_path"))

    writer = ManifestWriter(imported_root)
    writer.write(
        project_id=project_id,
        title="Bundle E2E",
        status="completed",
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        exports=ManifestExports(
            pages=[str(p) for p in page_pngs],
            webtoon=[str(p) for p in webtoon_paths],
            pdf=str(pdf_out),
        ),
        stats=ManifestStats(
            page_count=4,
            panel_count=8,
            generated_images=generated_images,
            regenerated_panels=0,
            average_qa_score=0.0,
        ),
    )

    # ===== Final verification =====
    manifest = json.loads((imported_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 4
    assert manifest["stats"]["panel_count"] == 8
    assert manifest["stats"]["generated_images"] == 8
    assert manifest["exports"]["pages"]
    assert manifest["exports"]["webtoon"]
    assert manifest["exports"]["pdf"] is not None
    assert "manga.pdf" in manifest["exports"]["pdf"]

    # Edited bubble text persisted.
    final_bubbles = json.loads((imported_root / "bubbles.json").read_text(encoding="utf-8"))
    final_target = next(b for b in final_bubbles if b["id"] == target_bubble_id)
    assert final_target["text"] == EDITED_TEXT

    # generation_log.json still valid.
    final_log = json.loads((imported_root / "generation_log.json").read_text(encoding="utf-8"))
    assert final_log["state"] == "COMPLETED"
    assert final_log["project_id"] == project_id
