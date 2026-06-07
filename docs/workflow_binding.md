# Workflow Binding

A *workflow* in Manga Autopilot is a JSON file in ComfyUI API format.
The **Workflow Registry** stores them on disk, validates them against
the live ComfyUI `object_info`, and binds typed inputs so the autopilot
and per-panel runners can call them with overrides.

## File layout

Each workflow is stored as a single file:

```text
{storage_root}/workflows/{workflow_id}.json
```

A sidecar index file ``workflows.json`` keeps a list of registered
workflow ids.

## API format workflows only

The registry rejects the UI-saved format. Save the workflow with
**"Save (API Format)"** in ComfyUI before uploading.

## Registering a workflow

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/workflows \
  -H 'Content-Type: application/json' \
  -d @workflows/anime_t2i_api.json
```

The response contains the assigned `workflow_id`. The validator runs
during registration and returns a per-node report.

## Bindings

A *binding* maps a workflow input key to a Manga Autopilot input type:

| Type        | Description                                    |
|-------------|------------------------------------------------|
| `prompt`    | Free-form text prompt (positive)               |
| `negative`  | Free-form negative prompt                      |
| `seed`      | Integer seed (auto-generated if not bound)     |
| `steps`     | Integer step count                             |
| `cfg`       | Float CFG                                      |
| `width`     | Integer width                                  |
| `height`    | Integer height                                 |
| `image`     | Reference image path                           |
| `lora`      | LoRA name + strength                           |

Bindings are part of the workflow JSON, e.g.:

```json
{
  "id": "anime_t2i",
  "name": "Anime Text-to-Image",
  "type": "t2i",
  "definition": { ... },
  "bindings": [
    {"input_key": "6.inputs.text", "type": "prompt", "required": true},
    {"input_key": "6.inputs.seed", "type": "seed", "required": false}
  ]
}
```

## Test-run

```bash
curl -X POST http://localhost:8188/manga_autopilot/api/workflows/anime_t2i/test-run \
  -H 'Content-Type: application/json' \
  -d '{"overrides": {"prompt": "1girl, blue hair, masterpiece"}}'
```

The runner applies overrides and submits a job to the local ComfyUI
server. The HTTP response is a JSON document describing the dispatch
status; image bytes are returned by ComfyUI through its standard
`/view` endpoint, not by this API.
