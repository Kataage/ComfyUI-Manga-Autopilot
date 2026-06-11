# Modal Worker MVP

This is an **experimental** Modal worker example for Manga Autopilot
remote GPU execution.  It is not used in CI and does not require a
Modal account or GPU.

## Status

- Skeleton / example only
- Follows the `RemoteHTTPExecutor` contract (`POST /v1/generate-panel`)
- No real ComfyUI or model execution in this MVP
- Modal SDK is optional — pure-Python helpers work without it

## Files

| File | Description |
|------|-------------|
| `modal_worker.py` | Pure-Python helpers + Modal stub (optional) |
| `modal_gpu_worker.py` | Real Modal GPU worker with HTTP endpoints |
| `requirements.txt` | Optional Modal SDK dependency |
| `README.md` | This file |

## Quick Start (no Modal SDK)

```bash
# Pure-Python helpers (no Modal SDK needed)
python examples/modal-worker/modal_worker.py
```

## Real Modal GPU Worker

### Setup

```bash
# Install Modal optional dependency
pip install -e ".[modal]"

# Authenticate with Modal
modal setup
```

### Deploy

```bash
# Deploy to Modal (creates a web endpoint)
modal deploy examples/modal-worker/modal_gpu_worker.py

# Or run locally for testing
modal serve examples/modal-worker/modal_gpu_worker.py
```

### Worker URL

After deploy, Modal provides a URL like:
```
https://your-app-name--your-username.modal.run
```

Set this as `MANGA_AUTOPILOT_MODAL_WORKER_URL` for opt-in tests.

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/generate-panel` | POST | Synchronous panel generation |
| `/v1/generate-panel-async` | POST | Async panel generation (returns job_id) |
| `/v1/jobs/{job_id}` | GET | Poll async job status |
| `/v1/jobs/{job_id}/cancel` | POST | Cancel a running job |

### Request Payload

```json
{
  "project_id": "project-xxx",
  "page_id": "page_0001",
  "panel_id": "panel_001_c00",
  "prompt": "hero standing in a city",
  "negative_prompt": "low quality, blurry",
  "seed": 12345,
  "width": 1024,
  "height": 1024,
  "workflow_id": "anime_t2i_default",
  "metadata": {
    "run_id": "run_20260609_123456_aabbccdd"
  }
}
```

### Response (image_base64)

```json
{
  "status": "completed",
  "filename": "panel_001_c00.png",
  "image_base64": "iVBORw0KGgo...",
  "seed": 12345,
  "metadata": {
    "executor": "modal-gpu-worker-mvp",
    "gpu": "T4"
  }
}
```

### Opt-in Test

```bash
MANGA_AUTOPILOT_REAL_MODAL_E2E=1 \
MANGA_AUTOPILOT_MODAL_WORKER_URL=https://your-app.modal.run \
pytest tests/backend/test_real_modal_worker_e2e.py -q
```

## Contract Tests (standard CI)

No Modal SDK or GPU required:

```bash
pytest tests/backend/test_modal_worker_contract.py -q
```

## Current Limitations

- MVP returns placeholder PNG images (no real ComfyUI execution)
- No model checkpoint loading
- No Modal Volume for model weights
- No Modal secrets for API keys
- No production-ready timeouts or cancel propagation
- In-memory job store (resets on cold start)

## What's Next

- Real ComfyUI workflow execution on Modal GPU
- Checkpoint / model volume setup
- Modal secrets for Civitai / HuggingFace tokens
- artifact_url delivery for large images
- run_id-based artifact naming
- Timeout / cancel propagation to Modal functions
- Production-ready GPU type configuration

---

## Modal ComfyUI Execution Worker

The `modal_comfyui_worker.py` file provides a real ComfyUI execution
path on Modal GPU.  It loads workflow registry JSON, injects bindings,
and executes the ComfyUI API workflow.

### Setup

```bash
# Install Modal optional dependency
pip install -e ".[modal]"

# Authenticate with Modal
modal setup

# Create volume for checkpoints and workflows
modal volume create manga-autopilot-comfyui
```

### Volume Structure

```
/modal-volumes/comfyui/
├── checkpoints/
│   └── example.safetensors
├── workflows/
│   └── anime_t2i_default.registry.json
└── outputs/
```

### Add Checkpoints

```bash
# Upload checkpoint to Modal Volume
modal volume put manga-autopilot-comfyui \
  /path/to/your/checkpoint.safetensors \
  /checkpoints/checkpoint.safetensors

# Upload workflow registry (optional, examples/workflows also works)
modal volume put manga-autopilot-comfyui \
  examples/workflows/anime_t2i_default.registry.json \
  /workflows/anime_t2i_default.registry.json
```

### Deploy

```bash
modal deploy examples/modal-worker/modal_comfyui_worker.py
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MANGA_AUTOPILOT_MODAL_VOLUME_NAME` | `manga-autopilot-comfyui` | Modal Volume name |
| `MANGA_MODAL_COMFYUI_ROOT` | `/root/ComfyUI` | ComfyUI installation root |
| `MANGA_MODAL_COMFYUI_PORT` | `8188` | ComfyUI server port |
| `MANGA_MODAL_COMFYUI_STARTUP_TIMEOUT` | `120` | Seconds to wait for ComfyUI |
| `MANGA_MODAL_COMFYUI_REQUEST_TIMEOUT` | `300` | Seconds for workflow execution |
| `MANGA_MODAL_OUTPUT_DIR` | `/outputs` | Output directory |

### Opt-in Test

```bash
MANGA_AUTOPILOT_REAL_MODAL_COMFYUI_E2E=1 \
MANGA_AUTOPILOT_MODAL_COMFYUI_WORKER_URL=https://your-app.modal.run \
MANGA_AUTOPILOT_MODAL_COMFYUI_WORKFLOW_JSON=examples/workflows/anime_t2i_default.registry.json \
pytest tests/backend/test_real_modal_comfyui_e2e.py -q
```

### Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `checkpoint not found: X` | Checkpoint not on Volume | Upload checkpoint to Modal Volume |
| `ComfyUI main.py not found` | ComfyUI not installed | Ensure ComfyUI is in the Modal image |
| `workflow registry not found` | Missing registry JSON | Upload registry or use examples/workflows |
| `ComfyUI server not ready` | Startup timeout | Increase `COMFYUI_STARTUP_TIMEOUT` |
| `no output image found` | Workflow failed | Check workflow graph and node connections |

### Contract Tests (standard CI)

```bash
pytest tests/backend/test_modal_comfyui_worker_contract.py -q
```

### Current Limitations (ComfyUI Worker)

- ComfyUI must be pre-installed in the Modal image
- Checkpoints must be manually placed on Modal Volume
- No automatic model cache or warm starts
- No production-ready auth or artifact storage
- In-memory ComfyUI subprocess (restarts each invocation)
- No long-running ComfyUI server optimization

---

## Preflight Validation

The `comfyui_preflight.py` module provides preflight checks for
validating the Modal ComfyUI environment before generation.

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/health` | GET | Basic health check (no ComfyUI required) |
| `/v1/preflight` | POST | Run all preflight validation checks |

### GET /v1/health

```bash
curl https://your-app.modal.run/v1/health
```

Response:
```json
{
  "status": "ok",
  "executor": "modal-comfyui",
  "comfyui_root": "/root/ComfyUI",
  "comfyui_root_exists": true,
  "volume_name": "manga-autopilot-comfyui",
  "volume_mounted": true,
  "output_dir": "/outputs",
  "comfyui_port": 8188
}
```

### POST /v1/preflight

```bash
curl -X POST https://your-app.modal.run/v1/preflight \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "anime_t2i_default",
    "checkpoint_name": "example.safetensors"
  }'
```

Response:
```json
{
  "ok": true,
  "executor": "modal-comfyui",
  "checks": [
    {"name": "env_MANGA_AUTOPILOT_MODAL_VOLUME_NAME", "ok": true, "message": "..."},
    {"name": "env_MANGA_MODAL_COMFYUI_ROOT", "ok": true, "message": "..."},
    {"name": "comfyui_root", "ok": true, "message": "..."},
    {"name": "checkpoints_dir", "ok": true, "message": "..."},
    {"name": "checkpoint_exists", "ok": true, "message": "..."},
    {"name": "workflow_id", "ok": true, "message": "..."},
    {"name": "workflow_bindings", "ok": true, "message": "..."},
    {"name": "workflow_api_graph", "ok": true, "message": "..."},
    {"name": "binding_positive_prompt", "ok": true, "message": "..."},
    {"name": "binding_negative_prompt", "ok": true, "message": "..."},
    {"name": "binding_seed", "ok": true, "message": "..."},
    {"name": "binding_width", "ok": true, "message": "..."},
    {"name": "binding_height", "ok": true, "message": "..."}
  ],
  "errors": []
}
```

### Validation Checks

| Check | Description |
|-------|-------------|
| `env_*` | Environment variable is set |
| `comfyui_root` | ComfyUI root directory exists |
| `checkpoints_dir` | Checkpoints directory exists |
| `checkpoint_exists` | Checkpoint file exists with size |
| `workflow_id` | Registry has workflow_id |
| `workflow_bindings` | Registry has bindings |
| `workflow_api_graph` | Registry has api_graph |
| `binding_*` | Required binding is present |

### Contract Tests (standard CI)

```bash
pytest tests/backend/test_modal_comfyui_preflight.py -q
```

### Common Preflight Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `MANGA_AUTOPILOT_MODAL_VOLUME_NAME is not set` | Env var missing | Set env var in Modal deploy |
| `MANGA_MODAL_COMFYUI_ROOT is not set` | Env var missing | Set env var in Modal deploy |
| `ComfyUI root not found` | Path doesn't exist | Check ComfyUI installation |
| `Checkpoints directory not found` | No models dir | Create dir on Volume |
| `checkpoint not found: X` | Checkpoint missing | Upload to Modal Volume |
| `missing required binding: positive_prompt` | Registry incomplete | Add binding to registry JSON |
