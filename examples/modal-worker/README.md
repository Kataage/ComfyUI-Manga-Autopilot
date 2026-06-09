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
