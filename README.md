# ComfyUI Manga Autopilot

A ComfyUI custom-node extension that drives an end-to-end pipeline for
producing short manga / Webtoon projects from a single idea: story
planning, character design, panel layout, image generation, QA scoring,
lettering, and export — all from inside ComfyUI.

This repository follows the design in
[`docs/comfyui_manga_autopilot_spec.md`](docs/comfyui_manga_autopilot_spec.md)
(v1.0.0).

## Features

- **Project + story planning** (LLM-driven, with JSON repair on failure)
- **Character Manager** with reference-image upload, IP-Adapter, and LoRA
  binding
- **Workflow Registry** with schema validation against the live ComfyUI
  `object_info` and a one-click test-run
- **Page / Panel editor** with template-based layouts and SVG/PNG rendering
- **Speech bubble** placement, vertical Japanese rendering, and PNG export
- **Candidate generation** with seed policies, **QA scoring**, and retry
  prompts (spec sections 17-18)
- **Autopilot state machine** with pause / resume / cancel and recovery
  strategies (spec section 7)
- **Export** to PNG pages, webtoon slices, PDF (A4/B5/Kindle/custom), and
  zipped project bundles
- **External GPU bridge** (Modal-style worker) with timeout-based fallback
  to the local ComfyUI server

## Requirements

- Python 3.10+
- ComfyUI 0.3.x or newer (the custom node speaks the API-format workflows)
- Pillow ≥ 10
- `aiohttp` ≥ 3.9
- `pydantic` ≥ 2
- `jsonschema` ≥ 4

## Installation

See [`docs/install.md`](docs/install.md) for the full guide.

```bash
# inside your ComfyUI installation
cd custom_nodes
git clone https://github.com/Kataage/ComfyUI-Manga-Autopilot
cd ComfyUI-Manga-Autopilot
pip install -e .
```

Restart ComfyUI. The "Manga Autopilot" tab appears in the UI.

## Quick Start

See [`docs/quickstart.md`](docs/quickstart.md).

```text
1. Launch ComfyUI.
2. Place this repo under custom_nodes/.
3. Install the dependencies.
4. Open the Manga Autopilot tab in the UI.
5. Register a sample workflow (workflows/anime_t2i_api.json).
6. Create a sample project and click "Export PNG".
```

## Sample workflows

The `workflows/` directory ships five API-format workflows you can register
out of the box:

- `anime_t2i_api.json`
- `anime_i2i_api.json`
- `anime_reference_api.json`
- `character_sheet_api.json`
- `upscale_api.json`

## Documentation

- [`docs/quickstart.md`](docs/quickstart.md)
- [`docs/install.md`](docs/install.md)
- [`docs/workflow_binding.md`](docs/workflow_binding.md)
- [`docs/character_consistency.md`](docs/character_consistency.md)
- [`docs/troubleshooting.md`](docs/troubleshooting.md)
- [`docs/contribution.md`](docs/contribution.md)
- [`docs/comfyui_manga_autopilot_spec.md`](docs/comfyui_manga_autopilot_spec.md)

## Status

Public APIs and folder layout may change before v1.0.0. The implementation
follows the spec above on a per-issue basis.

## License

This project is licensed under the terms in [LICENSE](LICENSE). Bundled
sample workflows are released under the same terms; their referenced model
files are not redistributed by this repository.
