# Quick Start

The shortest happy path through ComfyUI Manga Autopilot.

## 1. Start ComfyUI

```bash
python main.py --listen 0.0.0.0 --port 8188
```

## 2. Install the custom node

Follow [`docs/install.md`](docs/install.md). Restart ComfyUI afterwards.

## 3. Open the Manga Autopilot tab

Click the new **Manga Autopilot** tab in the ComfyUI sidebar.

## 4. Register a sample workflow

Click **Workflows → Register workflow** and pick one of the bundled
files under `workflows/` (e.g. `anime_t2i_api.json`). The validation
passes automatically when your ComfyUI server has the same nodes.

## 5. Create a sample project

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/projects \
  -H 'Content-Type: application/json' \
  -d '{
        "project_id": "demo",
        "title": "Demo",
        "idea": "A hero receives a black sword",
        "page_count": 4,
        "format": ["png_pages"]
      }'
```

## 6. Export a PNG

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/projects/demo/export/png \
  -H 'Content-Type: application/json' \
  -d '{"pages": {"page_1": [{"panel_id": "p1", "x": 16, "y": 16, "width": 600, "height": 400}]}}'
```

The PNG is written to
`{storage_root}/projects/demo/exports/pages/page_0001.png`.

## Next steps

- [`docs/workflow_binding.md`](docs/workflow_binding.md) — register your
  own workflows and bind inputs
- [`docs/character_consistency.md`](docs/character_consistency.md) — wire
  up reference images, IP-Adapter, and LoRA
- `docs/autopilot.md` — kick off a full LLM-driven run end-to-end
