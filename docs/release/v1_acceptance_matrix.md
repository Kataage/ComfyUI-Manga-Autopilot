# v1.0 Acceptance Matrix

This document defines the quality gate for the Manga Autopilot v1.0 release.
Every item marked **Done** must pass in CI before a release tag is created.

## Release Gate Commands

```bash
# Lint
ruff check .

# Full backend test suite (excluding opt-in real ComfyUI tests)
pytest tests/backend/ -q

# Release gate subset only
pytest -m release_gate -q
```

## Acceptance Matrix

### Core Pipeline

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| 1-page × 1-panel generation | Done | `test_one_page_autopilot_completes_end_to_end` | Yes | Fake executor, full autopilot |
| 2-page × 1-panel generation | Done | `test_two_page_autopilot_completes_end_to_end` | Yes | Fake executor |
| 4-page × 1-panel generation | Done | `test_four_page_autopilot_completes_end_to_end` | Yes | Fake executor |
| 1-page × 2-panel generation | Done | `test_multi_panel_per_page_autopilot_completes_end_to_end` | Yes | Fake executor |
| 1-page × 3-panel generation | Done | `test_three_panel_per_page_autopilot_completes_end_to_end` | Yes | Fake executor |
| 4-page × 2-panel generation | Done | `test_four_page_two_panel_autopilot_completes_end_to_end` | Yes | Fake executor |

### ComfyExecutor Path

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| ComfyExecutor + FakeComfyClient | Done | `test_one_page_autopilot_uses_comfy_executor_path` | Yes | FakeComfyClient, real ComfyExecutor |
| Real ComfyUI (opt-in) | Opt-in | `test_real_comfyui_executor_smoke_e2e` | No | Requires env vars + running server |

### Export

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Page PNG export | Done | `test_export_page_png_writes_file` | Yes | Unit test |
| Webtoon stitch + slice | Done | `test_webtoon_stitch_resizes_to_width`, `test_webtoon_slicer_splits` | Yes | Unit tests |
| PDF render | Done | `test_pdf_renderer_assembles` | Yes | Unit test |
| Webtoon/PDF in autopilot | Done | `test_four_page_two_panel_autopilot_completes_end_to_end` | Yes | E2E verifies exports dir |
| ExportService round-trip | Done | `test_export_service_png`, `test_export_service_pdf`, `test_export_service_all_exports` | Yes | Unit tests |
| Bundler round-trip | Done | `test_bundler_round_trip` | Yes | Unit test |

### Project Lifecycle

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Project re-edit (reopen + edit + re-render) | Done | `test_generated_project_can_be_reopened_edited_and_reexported` | Yes | Bubble PATCH + re-export |
| Failure → resume → complete | Done | `test_failed_autopilot_can_resume_missing_panels_only` | Yes | FailingAfterN executor |
| ZIP bundle export → import → edit → re-export | Done | `test_generated_project_bundle_can_be_imported_edited_and_reexported` | Yes | Different storage_root |

### Speech Bubbles

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Bubble layout + rendering | Done | `test_generated_project_can_be_reopened_edited_and_reexported` | Yes | E2E verifies bubble overlay |
| Bubble CRUD API | Done | `test_bubble_routes_*` | Yes | Unit tests in test_bubble_routes.py |

### Quality Assurance

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Generation loop (retry/fallback) | Done | `test_generation_job.py` (15 tests) | Yes | Unit tests |
| Prompt builder | Done | `test_prompt_builder.py` | Yes | Unit tests |
| LLM provider | Done | `test_llm_provider.py` | Yes | Unit tests |

### Security

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Zip Slip protection | Done | `test_export_security.py` (4 tests) | Yes | Path traversal tests |

## v1.0 Scope — Not Included

The following items are **out of scope** for v1.0 and remain in the Planned section:

| Area | Reason |
|------|--------|
| Web UI full editor | Frontend work deferred |
| History-aware editing UI | Requires undo/redo infrastructure |
| Diff preview | Depends on history-aware editing |
| ZIP import conflict resolution UI | Edge case, can be added post-release |
| External GPU worker default integration | GPUBridge exists but not wired to orchestrator |
| Real CLIP / IP-Adapter / face-sim QA | Requires model infrastructure |
| Complex panel layout AI | Beyond grid/fallback layouts |
| Real ComfyUI as required CI | Opt-in only, no GPU in standard CI |

### Examples / Starter Kit

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Example workflow registry payload valid | Done | `test_example_registry_payload_matches_workflow_definition` | Yes | `examples/workflows/anime_t2i_default.registry.json` |
| Example workflow binds all required keys | Done | `test_example_registry_has_required_bindings` | Yes | `positive_prompt`, `negative_prompt`, `seed`, `width`, `height`, `filename_prefix` |
| Example workflow registers in WorkflowRegistry | Done | `test_example_registry_can_be_registered` | Yes | `WorkflowRegistry.open()` round-trip |
| Example workflow runs with FakeComfyClient | Done | `test_example_registry_comfy_executor_e2e` | Yes | `ComfyExecutor.submit()` → overrides applied → image returned |

### Remote Executor Foundation

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| RemoteHTTPExecutor implements GenerationExecutor | Done | `test_autopilot_can_generate_panels_with_fake_remote_executor` | Yes | HTTP POST → base64 PNG → PIL Image |
| FakeRemoteWorker serves /v1/generate-panel | Done | `test_autopilot_can_generate_panels_with_fake_remote_executor` | Yes | In-process aiohttp server; deterministic PNG |
| Autopilot E2E completes via remote path | Done | `test_autopilot_can_generate_panels_with_fake_remote_executor` | Yes | 1-page / 1-panel; panels.json + jobs + generation_log.json + manifest |
| Request payload includes prompt/seed/width/height | Done | `test_autopilot_can_generate_panels_with_fake_remote_executor` | Yes | Verified in worker.requests[0] |
| Existing ComfyExecutor tests remain green | Done | `test_one_page_autopilot_uses_comfy_executor_path` | Yes | No regression |
| No external network service required | Done | — | Yes | FakeRemoteWorker is in-process |

### Remote Executor Hardening

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| HTTP 500 → RemoteExecutorHTTPError | Done | `test_remote_executor_raises_on_http_500` | Yes | Status code + body + URL in message |
| Timeout → RemoteExecutorTimeoutError | Done | `test_remote_executor_raises_on_timeout` | Yes | Timeout seconds + URL in message |
| Worker status error → RemoteExecutorResponseError | Done | `test_remote_executor_raises_on_status_error` | Yes | `{"status": "error", "error": "model not found"}` |
| Invalid JSON → RemoteExecutorResponseError | Done | `test_remote_executor_raises_on_invalid_json` | Yes | Non-JSON text response |
| Missing image_base64 → RemoteExecutorResponseError | Done | `test_remote_executor_raises_on_missing_image_base64` | Yes | JSON without image payload |
| Invalid base64 → RemoteExecutorImageError | Done | `test_remote_executor_raises_on_invalid_base64` | Yes | Non-base64 string |
| Invalid image bytes → RemoteExecutorImageError | Done | `test_remote_executor_raises_on_invalid_image_bytes` | Yes | Valid base64, not an image |
| API key Authorization header | Done | `test_remote_executor_sends_authorization_header` | Yes | `Bearer {api_key}` |
| Autopilot FAILED on remote error | Done | `test_autopilot_records_failure_when_remote_executor_fails` | Yes | generation_log.json has FAILED state |

### Remote Executor Async Polling

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| POST returns queued with job_id | Done | `test_remote_executor_polls_until_async_job_completed` | Yes | FakeRemoteWorker async_success mode |
| GET /v1/jobs/{job_id} polling until completed | Done | `test_remote_executor_polls_until_async_job_completed` | Yes | Running → completed transition |
| Job error → RemoteExecutorJobError | Done | `test_remote_executor_raises_when_async_job_errors` | Yes | Error status from poll response |
| Polling timeout → RemoteExecutorPollingTimeoutError | Done | `test_remote_executor_raises_when_async_job_polling_times_out` | Yes | Always-running job |
| max_poll_attempts limit | Done | `test_remote_executor_max_poll_attempts` | Yes | Limits number of GET requests |
| Sync completed path still works | Done | `test_sync_completed_still_works` | Yes | No regression |
| Autopilot E2E via async polling | Done | `test_autopilot_can_generate_panels_with_async_remote_executor` | Yes | 1-page / 1-panel; full pipeline |
| Autopilot FAILED on async job error | Done | `test_autopilot_records_failure_when_async_remote_job_errors` | Yes | generation_log.json has FAILED state |

### Modal Worker MVP

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Imports without Modal SDK | Done | `test_modal_worker_example_imports_without_modal_sdk` | Yes | Pure-Python helpers always available |
| Validates required payload fields | Done | `test_modal_worker_validates_required_payload_fields` | Yes | Raises ValueError on missing fields |
| Builds success response contract | Done | `test_modal_worker_builds_success_response_contract` | Yes | status=completed, image_base64, metadata.executor |
| Builds error response contract | Done | `test_modal_worker_builds_error_response_contract` | Yes | status=error, error message |
| Placeholder image is valid base64 | Done | `test_modal_worker_placeholder_image_is_valid_base64` | Yes | Decodes to PNG bytes |

### Remote Artifact URL Support

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Downloads image from artifact_url | Done | `test_remote_executor_downloads_artifact_url` | Yes | Sync artifact_url response |
| Reads image from artifact_path | Done | `test_remote_executor_reads_artifact_path` | Yes | Sync artifact_path response |
| Polls async artifact_url to completion | Done | `test_remote_executor_polls_until_async_artifact_url_completed` | Yes | Async queued → artifact_url |
| Raises on artifact_url 404 | Done | `test_remote_executor_raises_on_artifact_url_404` | Yes | HTTP 404 from artifact endpoint |
| Raises on missing artifact_path | Done | `test_remote_executor_raises_on_missing_artifact_path` | Yes | FileNotFoundError → RemoteExecutorImageError |
| Autopilot E2E with artifact_url | Done | `test_autopilot_can_generate_panels_with_artifact_url_remote_executor` | Yes | Full pipeline via artifact_url |

### PanelExecutionRequest Context

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| PanelExecutionRequest fields | Done | `test_panel_execution_request_fields` | Yes | All required fields present |
| effective_width/height fallback | Done | `test_panel_execution_request_effective_dimensions` | Yes | Falls back to prompt dimensions |
| Frozen (immutable) | Done | `test_panel_execution_request_frozen` | Yes | Cannot modify after creation |
| GenerationLoop builds request | Done | `test_generation_loop_builds_panel_execution_request_context` | Yes | project/page/panel/candidate context |
| Remote payload includes all IDs | Done | `test_remote_executor_payload_includes_project_page_panel_and_candidate_ids` | Yes | page_id is non-empty |
| RemoteGenerateRequest includes candidate_id | Done | `test_remote_generate_request_includes_candidate_id` | Yes | candidate_id in to_dict() |

### Cancel API Foundation

| Area | Status | Test | CI | Notes |
|------|--------|------|----|-------|
| Cancel endpoint writes cancel.json marker | Done | `test_cancel_endpoint_writes_cancel_marker` | Yes | cancel.json saved to project root |
| GenerationLoop stops when cancel marker exists | Done | `test_cancel_marker_detected_by_generation_loop` | Yes | Job status = CANCELLED |
| GenerationLoop completes without cancel marker | Done | `test_generation_loop_completes_without_cancel_marker` | Yes | No regression |
| RemoteHTTPExecutor.cancel calls POST /v1/jobs/{job_id}/cancel | Done | `test_remote_executor_cancel_calls_endpoint` | Yes | FakeRemoteWorker records cancel request |
| RemoteHTTPExecutor raises RemoteExecutorCancelledError | Done | `test_remote_executor_raises_when_polled_job_is_cancelled` | Yes | Polling detects cancelled status |
| Autopilot cancel during remote polling | Done | `test_autopilot_cancel_during_remote_polling` | Yes | Fake remote worker only; real Modal/RunPod cancel not implemented |

## How to Use

### Before every release

```bash
ruff check .
pytest tests/backend/ -q
pytest -m release_gate -q
```

### With real ComfyUI (manual)

```bash
MANGA_AUTOPILOT_REAL_COMFY_E2E=1 \
MANGA_AUTOPILOT_COMFY_BASE_URL=http://127.0.0.1:8188 \
MANGA_AUTOPILOT_TEST_WORKFLOW_JSON=/path/to/workflow_api.json \
pytest tests/backend/test_real_comfy_executor_e2e.py -q
```

### CI (GitHub Actions)

The `.github/workflows/ci.yml` runs on every push and PR:

- `ruff check .`
- `pytest tests/backend/ -q`

Real ComfyUI tests are **never** run in standard CI.
