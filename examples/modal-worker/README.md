# Modal Worker MVP

This is an **experimental** Modal worker example for Manga Autopilot
remote GPU execution.  It is not used in CI and does not require a
Modal account or GPU.

## Status

- Skeleton / example only
- Follows the `RemoteHTTPExecutor` contract (`POST /v1/generate-panel`)
- No real ComfyUI or model execution in this MVP
- Modal SDK is optional — pure-Python helpers work without it

## Usage

```bash
# Pure-Python helpers (no Modal SDK needed)
python examples/modal-worker/modal_worker.py --dry-run

# With Modal SDK installed (not used in CI)
pip install modal
modal deploy examples/modal-worker/modal_worker.py
```

## Files

| File | Description |
|------|-------------|
| `modal_worker.py` | Worker functions + Modal stub (optional) |
| `requirements.txt` | Optional Modal SDK dependency |
| `README.md` | This file |

## Contract

The worker is expected to handle `POST /v1/generate-panel` with the
same request/response format as `RemoteHTTPExecutor`:

- Request: `project_id`, `panel_id`, `prompt`, `seed`, `width`, `height`, etc.
- Success response: `status=completed`, `image_base64`, `filename`, `metadata`
- Error response: `status=error`, `error`

See `examples/remote-worker/README.md` for the full contract.

## Before production use

- Configure checkpoint / model path
- Set up Modal volumes for model weights
- Configure Modal secrets (if needed)
- Set appropriate timeouts and GPU type
- Test with real ComfyUI workflow on Modal GPU
