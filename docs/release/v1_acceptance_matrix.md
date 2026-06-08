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
