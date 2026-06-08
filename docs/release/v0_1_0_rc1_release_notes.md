# Manga Autopilot v0.1.0-rc1 — Release Notes

**Release Candidate 1** — the first public preview of the full
Manga Autopilot pipeline.

## What's included

Manga Autopilot is a ComfyUI custom-node extension that takes a
single idea and produces a short manga / Webtoon — story structure,
characters, panel layout, image generation, QA, lettering, and export
— all from inside ComfyUI.

### Core pipeline

- **Autopilot** drives the full pipeline from a single prompt:
  story planning → character design → page / panel layout → prompt
  generation → image generation (with candidate selection, QA scoring,
  retry, and fallback) → lettering → page rendering → export.
- **Multi-page, multi-panel** support: 1–4 pages, 1–3 panels per page.
- **Speech bubbles** with auto-layout, vertical Japanese text, and
  overlay rendering on exported pages.

### Export formats

- **Page PNGs** — individual page images at 1200×1600.
- **Webtoon** — vertically stitched full webtoon + per-page slices.
- **PDF** — A4 / B5 / Kindle / custom sizes with configurable margins.
- **ZIP bundles** — portable project archives for backup / migration.

### E2E test coverage

| Test | Coverage |
|------|----------|
| 1-page × 1-panel | Full autopilot happy path |
| 4-page × 2-panel | Multi-page + multi-panel |
| ComfyExecutor + FakeComfyClient | ComfyUI transport layer |
| Real ComfyUI (opt-in) | Live server smoke test |
| Project re-edit | Reopen → edit → re-render → re-export |
| Failure resume | Mid-run failure → resume → complete |
| ZIP bundle import | Export → import to different storage → edit → re-export |

500 tests pass in standard CI (1 skipped: real ComfyUI opt-in).

## Installation

### As a ComfyUI custom node

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/Kataage/ComfyUI-Manga-Autopilot
cd ComfyUI-Manga-Autopilot
pip install -e .
```

Restart ComfyUI; a "Manga Autopilot" sidebar tab appears in the UI.

### Local smoke test

```bash
python -m pip install -e .
ruff check .
pytest -m release_gate -q
```

### Real ComfyUI (opt-in)

```bash
MANGA_AUTOPILOT_REAL_COMFY_E2E=1 \
MANGA_AUTOPILOT_COMFY_BASE_URL=http://127.0.0.1:8188 \
MANGA_AUTOPILOT_TEST_WORKFLOW_JSON=/path/to/workflow_api.json \
pytest tests/backend/test_real_comfy_executor_e2e.py -q
```

## Requirements

- Python 3.10+
- ComfyUI 0.3.x+
- Pillow 10+
- aiohttp 3.9+
- pydantic 2+
- jsonschema 4+

## Known limitations

- Web UI full editor is not included (bubble text editable via HTTP
  API only).
- External GPU worker is not wired as the default path.
- Real CLIP / IP-Adapter / face-similarity QA is not implemented.
- Complex panel layout AI is not implemented.
- ZIP import conflict resolution is not implemented.

## What's next

- Web UI visual editor for projects, panels, and bubbles.
- History-aware editing with undo / redo.
- Diff preview for re-rendered pages.
- External GPU worker as default executor.
- Real-image QA scoring (CLIP / IP-Adapter).

## Links

- [Repository](https://github.com/Kataage/ComfyUI-Manga-Autopilot)
- [Acceptance Matrix](v1_acceptance_matrix.md)
- [Release Checklist](v1_release_checklist.md)
- [Changelog](../../CHANGELOG.md)
