# Sample Workflows

Reference ComfyUI API-format workflows shipped with Manga Autopilot. Each
file can be registered through `POST /manga_autopilot/api/workflows` (the
binder UI also accepts drag-and-drop).

| File | workflow_id | Type | Purpose |
|---|---|---|---|
| `anime_t2i_api.json` | `anime_t2i_default` | `text_to_image` | Default SDXL/SD1.5 anime text-to-image pipeline |
| `anime_i2i_api.json` | `anime_i2i_default` | `image_to_image` | Variation / redraw using a previous panel |
| `anime_reference_api.json` | `anime_reference_default` | `reference_to_image` | IP-Adapter reference-based generation |
| `character_sheet_api.json` | `character_sheet_default` | `character_sheet` | 2x2 character reference sheet |
| `upscale_api.json` | `upscale_default` | `upscale` | 2x upscale + detail-recovery pass |

## Usage

```bash
# Register a workflow
curl -X POST http://localhost:8188/manga_autopilot/api/workflows \
  -H 'Content-Type: application/json' \
  --data @workflows/anime_t2i_api.json

# Trigger a test-run
curl -X POST \
  http://localhost:8188/manga_autopilot/api/workflows/anime_t2i_default/test-run \
  -H 'Content-Type: application/json' \
  -d '{"positive_prompt": "a hero standing in the rain"}'
```

## Customisation

Each file declares:

- `workflow_id`, `name`, `type`, `file` — registry metadata
- `bindings` — maps Manga Autopilot field names to ComfyUI node inputs
- `api_graph` — the ComfyUI `/prompt` graph (optional but required for
  test-runs to actually submit to ComfyUI)

Edit either field set in the binder UI (`Workflow Binder` page) and click
**Save** to persist changes. See
[`docs/comfyui_manga_autopilot_spec.md`](../docs/comfyui_manga_autopilot_spec.md)
section 12 for the binding contract.
