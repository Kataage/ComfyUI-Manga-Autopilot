# Remote Worker Contract

This document describes the HTTP contract for a Manga Autopilot remote
GPU worker.  Implement this endpoint to receive panel generation
requests from `RemoteHTTPExecutor`.

The executor supports two modes:

- **Synchronous**: single POST returns completed image
- **Asynchronous**: POST returns job_id; executor polls GET until done

The response can deliver images via three formats (in priority order):

- **`image_base64`**: inline base64 (MVP default)
- **`artifact_url`**: URL to download the image (recommended for large images)
- **`artifact_path`**: local file path (for local/test workers)

## Endpoint

```
POST /v1/generate-panel
```

## Request

```json
{
  "project_id": "project-xxx",
  "page_id": "page_0001",
  "panel_id": "panel_001",
  "candidate_id": "panel_001_c00",
  "prompt": "hero standing in a city",
  "negative_prompt": "low quality, blurry",
  "seed": 12345,
  "width": 1024,
  "height": 1024,
  "workflow_id": "anime_t2i_default",
  "metadata": {
    "run_id": "run_20260609_123456_aabbccdd",
    "attempt_index": 0
  }
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `project_id` | string | yes | Project identifier |
| `page_id` | string | yes | Page identifier (e.g. `page_0001`) |
| `panel_id` | string | yes | Panel identifier (e.g. `panel_001`) |
| `candidate_id` | string | yes | Candidate identifier (e.g. `panel_001_c00`) |
| `prompt` | string | yes | Positive prompt text |
| `negative_prompt` | string | no | Negative prompt text |
| `seed` | integer | yes | RNG seed |
| `width` | integer | yes | Image width in pixels |
| `height` | integer | yes | Image height in pixels |
| `workflow_id` | string | no | Workflow registry identifier |
| `metadata` | object | no | Arbitrary metadata (includes `run_id`, `attempt_index`) |

## Sync Response

Return the image directly.  Use one of three formats:

### image_base64 (MVP default)

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

### artifact_url (recommended for large images)

```json
{
  "status": "completed",
  "filename": "panel_001_c00.png",
  "artifact_url": "http://127.0.0.1:9000/artifacts/panel_001_c00.png",
  "seed": 12345,
  "metadata": {
    "executor": "your-worker-name"
  }
}
```

The executor will HTTP GET the `artifact_url` to download the image.
Use this for S3 / R2 / Modal Volume / HTTP file server integrations.

### artifact_path (local/test workers)

```json
{
  "status": "completed",
  "filename": "panel_001_c00.png",
  "artifact_path": "/tmp/worker-output/panel_001_c00.png",
  "seed": 12345,
  "metadata": {
    "executor": "your-worker-name"
  }
}
```

The executor reads the local file at `artifact_path`.
Use this for local test workers or CI environments.

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

Or with `artifact_url`:

```json
{
  "status": "completed",
  "job_id": "job_abc123",
  "filename": "panel_001_c00.png",
  "artifact_url": "http://127.0.0.1:9000/artifacts/panel_001_c00.png",
  "seed": 12345,
  "metadata": {
    "executor": "your-worker-name"
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

## Cancel Endpoint

```
POST /v1/jobs/{job_id}/cancel
```

Tells the worker to stop processing the given job.  The worker should
transition the job to `cancelled` status.

### Cancel Request

```json
{
  "reason": "user requested cancellation"
}
```

### Cancel Response

```json
{
  "status": "cancelled",
  "job_id": "job_abc123"
}
```

### Polling after cancel

When a job has been cancelled, the polling endpoint returns:

```json
{
  "status": "cancelled",
  "job_id": "job_abc123"
}
```

The executor will raise `RemoteExecutorCancelledError`.

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

- `page_id` is non-empty in the request payload (e.g. `page_0001`).
- `image_base64` is the MVP format.  For large images, prefer
  `artifact_url` (S3 / R2 / Modal Volume / HTTP file server).
- `artifact_path` is for local/test workers only; not recommended for
  production remote workers.
- `run_id` can be used for artifact naming (e.g. `{run_id}/{page_id}/{candidate_id}.png`).
- Per-run artifacts are mirrored under `runs/{run_id}/` after completion.
- No Modal SDK or RunPod integration is required for this contract.
- Real S3 / R2 / Modal Volume integration is not yet implemented.
- Cancel is a foundation; real Modal/RunPod cancel is not yet implemented.
- Partial assets may remain after cancellation.

## See also

- [`../modal-worker/README.md`](../modal-worker/README.md) — Modal worker MVP example
