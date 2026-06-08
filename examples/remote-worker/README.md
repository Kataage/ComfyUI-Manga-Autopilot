# Remote Worker Contract

This document describes the HTTP contract for a Manga Autopilot remote
GPU worker.  Implement this endpoint to receive panel generation
requests from `RemoteHTTPExecutor`.

## Endpoint

```
POST /v1/generate-panel
```

## Request

```json
{
  "project_id": "project-xxx",
  "page_id": "",
  "panel_id": "panel_001_c00",
  "prompt": "hero standing in a city",
  "negative_prompt": "low quality, blurry",
  "seed": 12345,
  "width": 1024,
  "height": 1024,
  "workflow_id": "anime_t2i_default",
  "metadata": {}
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `project_id` | string | yes | Project identifier |
| `page_id` | string | no | Page identifier (empty in v0.1) |
| `panel_id` | string | yes | Panel + candidate identifier |
| `prompt` | string | yes | Positive prompt text |
| `negative_prompt` | string | no | Negative prompt text |
| `seed` | integer | yes | RNG seed |
| `width` | integer | yes | Image width in pixels |
| `height` | integer | yes | Image height in pixels |
| `workflow_id` | string | no | Workflow registry identifier |
| `metadata` | object | no | Arbitrary metadata |

## Success Response

```json
{
  "status": "completed",
  "filename": "panel_001_c00.png",
  "image_base64": "iVBORw0KGgo...",
  "seed": 12345,
  "metadata": {
    "executor": "your-worker-name",
    "prompt_id": "optional-prompt-id"
  }
}
```

## Error Response

When the worker cannot generate the image, return HTTP 200 with an
error status:

```json
{
  "status": "error",
  "error": "model not found"
}
```

The executor will raise `RemoteExecutorResponseError`.

Alternatively, return HTTP 500 for server-side errors.  The executor
will raise `RemoteExecutorHTTPError`.

## Authentication

If `api_key` is set in `RemoteWorkerSettings`, the executor sends:

```
Authorization: Bearer {api_key}
```

Validate this header in your worker if needed.

## Timeout

The executor has a configurable `timeout_sec` (default 60s).  If the
worker does not respond within this window, the executor raises
`RemoteExecutorTimeoutError`.

## Notes

- `page_id` is empty string in v0.1 — the `GenerationExecutor` protocol
  does not carry page context.  Future versions will add formal `page_id`.
- `image_base64` is the MVP format.  Future versions may use artifact
  URLs (S3 / R2 signed URLs) instead.
- No Modal SDK or RunPod integration is required for this contract.
