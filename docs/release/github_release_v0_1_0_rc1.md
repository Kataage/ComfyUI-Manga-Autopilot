# v0.1.0-rc1 — Autopilot pipeline release candidate

First release candidate of Manga Autopilot — a ComfyUI custom-node
extension that takes a single idea and produces a short manga /
Webtoon end-to-end.

## Summary

This release covers the full v1.0 spec pipeline: story planning,
character design, page/panel layout, image generation with QA and
retry, speech bubbles, page rendering, and export to PNG / Webtoon /
PDF.  The pipeline is driven by an Autopilot state machine with pause,
resume, cancel, and failure-recovery support.

**500 tests pass in standard CI.  6 key E2E tests are gated behind
the `release_gate` marker.**

## What works

- **Autopilot pipeline** — story → characters → pages → panels →
  prompts → generation → QA → lettering → render → export.
- **Multi-page / multi-panel** — 1–4 pages, 1–3 panels per page.
  Tested: 1p1c, 2p1c, 4p1c, 1p2c, 1p3c, 4p2c.
- **Speech bubbles** — auto layout, vertical Japanese, PNG overlay.
- **Export** — page PNGs, webtoon (stitched + sliced), PDF (A4/B5/
  Kindle/custom), ZIP project bundles.
- **ComfyExecutor path** — real executor + fake ComfyClient E2E
  without a live server.
- **Real ComfyUI opt-in** — env-var-gated smoke test against a live
  ComfyUI instance.
- **Project re-edit** — reopen, edit bubble text, re-render, re-export.
- **Failure resume** — mid-run failure → resume → complete, skipping
  already-generated panels.
- **ZIP bundle import** — export to ZIP, import to a different storage
  root, edit, re-render, re-export.
- **HTTP API** — full REST surface for projects, panels, bubbles,
  characters, workflows, autopilot, and exports.
- **Web extension** — sidebar tab with project picker, page editor,
  character manager, progress monitor, export center.

## Release gate results

```
$ ruff check .
All checks passed

$ pytest tests/backend/ -q
500 passed, 1 skipped, 26 warnings

$ pytest -m release_gate -v
6 passed, 495 deselected
```

| Test | Coverage |
|------|----------|
| `test_one_page_autopilot_completes_end_to_end` | 1-page × 1-panel generation |
| `test_four_page_two_panel_autopilot_completes_end_to_end` | 4-page × 2-panel + webtoon/PDF |
| `test_one_page_autopilot_uses_comfy_executor_path` | ComfyExecutor + FakeComfyClient |
| `test_generated_project_can_be_reopened_edited_and_reexported` | Project re-edit round-trip |
| `test_failed_autopilot_can_resume_missing_panels_only` | Failure → resume → complete |
| `test_generated_project_bundle_can_be_imported_edited_and_reexported` | ZIP bundle import round-trip |

## Install / local smoke test

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

## Real ComfyUI opt-in E2E

The real ComfyUI test is **skipped by default**.  To run it against a
live ComfyUI server:

```bash
MANGA_AUTOPILOT_REAL_COMFY_E2E=1 \
MANGA_AUTOPILOT_COMFY_BASE_URL=http://127.0.0.1:8188 \
MANGA_AUTOPILOT_TEST_WORKFLOW_JSON=/path/to/workflow_api.json \
pytest tests/backend/test_real_comfy_executor_e2e.py -q
```

## Known limitations

The following are **not included** in this release candidate:

- **Full Web UI editor** — bubble text is editable via HTTP API only;
  a visual editor is planned.
- **External GPU worker default integration** — `GPUBridge` exists but
  is not wired as the default executor path.
- **Real CLIP / IP-Adapter / face-similarity QA** — the QA loop runs
  heuristics only; model-based scoring is on the roadmap.
- **Complex panel layout AI** — beyond grid / fallback layouts.
- **ZIP import conflict resolution UI** — merge / overwrite / skip UI
  for overlapping imports.
- **Real ComfyUI as required CI** — opt-in only; no GPU required for
  standard CI.

## Next steps

- Web UI visual editor for projects, panels, and bubbles.
- History-aware editing with undo / redo.
- Diff preview for re-rendered pages.
- External GPU worker as default executor.
- Real-image QA scoring (CLIP / IP-Adapter).

## Links

- [CHANGELOG.md](CHANGELOG.md)
- [Release notes](docs/release/v0_1_0_rc1_release_notes.md)
- [Acceptance matrix](docs/release/v1_acceptance_matrix.md)
- [Release checklist](docs/release/v1_release_checklist.md)
- [Repository](https://github.com/Kataage/ComfyUI-Manga-Autopilot)
