# Install

This guide covers installing **ComfyUI Manga Autopilot** as a ComfyUI
custom node.

## 1. Prerequisites

- Python 3.10 or newer
- A working [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
  installation
- The model files referenced by the bundled sample workflows (place them
  under your ComfyUI `models/` directory)
- An LLM endpoint if you want the LLM-driven planners
  (Ollama, OpenAI-compatible, or use the `manual` provider to enter JSON
  by hand)

## 2. Clone

```bash
cd path/to/ComfyUI/custom_nodes
git clone https://github.com/Kataage/ComfyUI-Manga-Autopilot
cd ComfyUI-Manga-Autopilot
```

## 3. Install dependencies

```bash
pip install -e .
```

The install pulls in `aiohttp`, `pydantic`, `PyYAML`, `Pillow`, and
`jsonschema`. For local development add the dev extras:

```bash
pip install -e ".[dev]"
```

## 4. Restart ComfyUI

The custom node registers an HTTP API under
`/manga_autopilot/api/...` and adds a "Manga Autopilot" tab to the
ComfyUI web UI.

## 5. Verify

```bash
curl http://localhost:8188/manga_autopilot/api/health
# -> {"status": "ok"}
```

## 6. Optional: external GPU worker

See `docs/modal_bridge.md` (or `src/manga_autopilot/services/gpu_bridge.py`)
for the worker request/response contract. The bridge is opt-in — leaving
`worker.endpoint` empty forces local generation.

## Troubleshooting

If routes do not appear:

- Confirm `WEB_DIRECTORY` resolves to `web/` inside this repo
- Check the ComfyUI console for `manga_autopilot.routes` errors
- Run `python -c "import manga_autopilot"` from the ComfyUI venv to
  confirm the package imports
