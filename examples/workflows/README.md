# Workflow Examples

## Files

| File | Description |
|------|-------------|
| `anime_t2i_default.workflow.json` | ComfyUI API-format workflow graph (minimal text-to-image) |
| `anime_t2i_default.registry.json` | WorkflowRegistry payload (workflow_id + bindings + api_graph) |

## How to use with Real ComfyUI

### 1. Export your workflow from ComfyUI

In ComfyUI, click **Save (API Format)** to export the workflow as a
JSON file.  This gives you the `api_graph` portion.

### 2. Adapt the registry payload

Open `anime_t2i_default.registry.json` and:

1. Replace the `api_graph` value with your exported workflow graph.
2. Update the `bindings` section so each `node_id` matches the
   corresponding node in your workflow:

| Binding key | What it maps to | Example node_id |
|-------------|-----------------|-----------------|
| `positive_prompt` | CLIPTextEncode (positive) | `"6"` |
| `negative_prompt` | CLIPTextEncode (negative) | `"7"` |
| `seed` | KSampler seed input | `"3"` |
| `width` | EmptyLatentImage width | `"5"` |
| `height` | EmptyLatentImage height | `"5"` |
| `filename_prefix` | SaveImage filename | `"9"` |

3. Update `file` to point to your workflow file path.

### 3. Register the workflow

Either:
- Place the JSON in your ComfyUI `workflows/` directory, or
- Use the HTTP API: `POST /manga_autopilot/api/workflows` with the
  registry payload as the JSON body.

### 4. Run the Real ComfyUI opt-in E2E

```bash
MANGA_AUTOPILOT_REAL_COMFY_E2E=1 \
MANGA_AUTOPILOT_COMFY_BASE_URL=http://127.0.0.1:8188 \
MANGA_AUTOPILOT_TEST_WORKFLOW_JSON=examples/workflows/anime_t2i_default.registry.json \
pytest tests/backend/test_real_comfy_executor_e2e.py -q
```

## Important notes

- **Checkpoint name**: The example workflow uses
  `example.safetensors` as a placeholder.  You **must** change
  `ckpt_name` in the `api_graph` to a checkpoint that exists on your
  ComfyUI server.
- **Custom nodes**: If your workflow uses custom nodes (e.g.
  ControlNet, IP-Adapter), those nodes must also be installed on the
  ComfyUI server.
- **CI**: Real ComfyUI E2E tests are **never** run in standard CI.
  They require a live ComfyUI server and are opt-in only.
- **Low-spec machines**: The standard test suite (`pytest tests/backend/
  -q`) uses fake executors and requires no GPU.
