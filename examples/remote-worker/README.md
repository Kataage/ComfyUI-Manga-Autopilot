# Remote Worker Contract

This document describes the HTTP contract for a Manga Autopilot remote
GPU worker.  Implement this endpoint to receive panel generation
requests from `RemoteHTTPExecutor`.

The executor supports two modes:

- **Synchronous**: single POST returns completed image
- **Asynchronous**: POST returns job_id; executor polls GET until done

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

## Sync Response

Return the image directly:

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

## Async Response

Return a job_id for later polling:

```json
{
  "status": "queued",
  "job_id": "job_abc123",
  "metadata": {
    "executor": "your-worker-name"
  }
}
```

Valid async initial statuses: `queued`, `accepted`, `running`.

## Polling Endpoint

```
GET /v1/jobs/{job_id}
```

### Running

```json
{
  "status": "running",
  "job_id": "job_abc123"
}
```

### Completed

```json
{
  "status": "completed",
  "job_id": "job_abc123",
  "filename": "panel_001_c00.png",
  "image_base64": "iVBORw0KGgo...",
  "seed": 12345,
  "metadata": {
    "executor": "your-worker-name",
    "prompt_id": "optional-prompt-id"
  }
}
```

### Error

```json
{
  "status": "error",
  "job_id": "job_abc123",
  "error": "model not found"
}
```

The executor will raise `RemoteExecutorJobError`.

## Error Response (sync)

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

## Timeouts

- `timeout_sec`: HTTP request timeout for individual POST/GET calls
  (default 60s).  Raises `RemoteExecutorTimeoutError`.
- `poll_timeout_sec`: Maximum time to poll a job before giving up
  (default 60s).  Raises `RemoteExecutorPollingTimeoutError`.
- `poll_interval_sec`: Delay between poll attempts (default 0.1s).
- `max_poll_attempts`: Optional cap on number of poll requests.

## Notes

- `page_id` is empty string in v0.1 — the `GenerationExecutor` protocol
  does not carry page context.  Future versions will add formal `page_id`.
- `image_base64` is the MVP format.  Future versions may use artifact
  URLs (S3 / R2 signed URLs) instead.
- No Modal SDK or RunPod integration is required for this contract.
