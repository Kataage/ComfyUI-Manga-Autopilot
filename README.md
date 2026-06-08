# ComfyUI Manga Autopilot

A ComfyUI custom-node extension that takes a single idea and produces a
short manga / Webtoon — story structure, characters, panel layout, image
generation, QA, lettering, and export — all from inside ComfyUI.

The implementation follows
[`docs/comfyui_manga_autopilot_spec.md`](docs/comfyui_manga_autopilot_spec.md)
(v1.0.0).

---

## Status (v0.x → v1.0.0)

This section is the source of truth for what actually works today. The
feature lists below are split into **Implemented** (covered by tests and
the HTTP API) and **Planned** (described in the spec, scheduled for a
later issue, not yet wired up).

### Implemented

- **HTTP API surface** — `/manga_autopilot/api/{health,workflows,…}` is
  registered on the ComfyUI `PromptServer` Application. Every handler
  resolves `manga_storage_root` and `manga_workflow_registry` on the
  Application context, so character / export / workflow routes do not 500
  after ComfyUI starts.
- **Workflow registry** — register, list, get, update, delete; per-type
  required-binding validation (`text_to_image` requires
  `positive_prompt/negative_prompt/seed/width/height` and one of
  `output_node` / `filename_prefix`; `upscale` requires `reference_image`;
  `reference_to_image` / `image_to_image` add `reference_image`; etc.).
- **Workflow test-run** — `/workflows/{id}/test-run` submits the workflow
  to ComfyUI via `ComfyClient.submit_workflow`, polls `/history/{prompt_id}`
  for completion, and downloads images via `/view` to
  `{storage_root}/test_runs/{workflow_id}/`.
- **ComfyClient** — full transport layer for `/prompt`, `/history/{id}`,
  `/view`, `/upload/image`, `/object_info`, `/system_stats`, `/devices`,
  `/extensions`, WebSocket `/ws` event stream.
- **Project storage layout** — `ensure_storage_root` /
  `ensure_project_paths` create the on-disk layout from spec §9.1.
- **Character service** — CRUD, reference image upload (with size /
  extension validation), `build_character_prompt`, IP-Adapter and LoRA
  overrides, sheet view helpers, character card export.
- **Speech bubble service** — CRUD, layout placement with classifier
  rotation, PNG bubble renderer.
- **Page renderer** — composites panel borders **and** the generated
  images into the page PNG (`cover` / `contain` / `stretch` fit modes,
  optional rotation).
- **Export service** — PNG pages, webtoon stitching + slicing, PDF
  (A4/B5/Kindle/custom, margins, DPI), and a project bundler.
- **Export path safety** — `ExportService.resolve_page_pngs` rejects any
  path outside the project's storage tree.
- **Project importer** — safe zip extraction (Zip Slip protection:
  absolute paths and `..` segments are rejected).
- **Autopilot** — state machine with 16 happy-path + 8 failure states,
  error recovery table, `AutopilotController` for pause / resume /
  cancel, and an `Orchestrator` that drives each step through
  injectable hooks (story → pages → panels → prompts → workflow →
  panels → QA → lettering → render → export → finalize).  HTTP
  `/autopilot/{start,pause,resume,cancel,status}` routes kick off the
  orchestrator in a background `asyncio.Task`.  The orchestrator honours
  pause **at the start of every step** by awaiting an `asyncio.Event`;
  a `resume` call rewinds the state machine to the pre-pause state and
  unblocks the wait.  Mid-step pause is not automatic — hooks that may
  run for a long time should observe `run.pause_event` themselves if
  they need to bail out faster.
- **1-page v1.0 happy path** — `POST /projects` → `POST /autopilot/start`
  with `page_count=1` walks the full default pipeline: `StoryPlanner`
  → `CharacterPlanner` → `PagePlanner` → `PanelPlanner` →
  `PromptBuilder` → `GenerationLoop` (candidate → executor → QA → retry
  → fallback) → `PageRenderer` → `ExportService` → `ManifestWriter`.
  Each panel image lands in `assets/panels/`, the page render lands in
  `exports/pages/page_0001.png`, and `manifest.json` +
  `generation_log.json` are written.  A 1-page end-to-end integration
  test (`test_one_page_e2e.py`) exercises this against fake LLM / fake
  executor and verifies every artefact on disk.
- **Multi-page / multi-panel autopilot** — `page_count` and
  `panels_per_page` parameters drive the orchestrator to create
  multiple `PanelRecord`s per page; each panel gets its own
  `GenerationJob`, `SpeechBubble`, and fallback layout.  Pages are
  rendered via `export_page_png` into `exports/pages/page_NNNN.png`.
  End-to-end tests cover 1-page/2-panel, 1-page/3-panel,
  2-page/1-panel, 4-page/1-panel, and 4-page/2-panel configurations.
- **Project + panel HTTP APIs** — `GET/POST /projects`,
  `GET/PATCH/DELETE /projects/{id}`, `GET /projects/_suggest_id`
  (spec §21.2) and `POST /panels/{id}/{generate,regenerate,repair}`,
  `PATCH /panels/{id}`, `GET /panels/{id}` (spec §21.6).  Generation
  endpoints persist a `GenerationJob` (status / candidates / selected
  candidate / retries) to `jobs/{job_id}.json` and update the
  underlying `PanelRecord` with the new `image_path` + history.
- **Web extension** — sidebar tab, project picker, page editor,
  character manager, progress monitor, export center, all mounted from
  `web/index.js`.
- **ComfyExecutor E2E** — a fake `ComfyClient` + real `WorkflowRegistry`
  + real `ComfyExecutor` path exercises `/prompt` → `/history` → `/view`
  end-to-end without a live ComfyUI server.  Workflow binding overrides
  (positive/negative/seed/width/height) are verified on the submitted
  graph (`test_comfy_executor_e2e.py`).
- **Real ComfyUI E2E (opt-in).**  An environment-variable-gated smoke
  test (`test_real_comfy_executor_e2e.py`) exercises the full
  autopilot → `ComfyExecutor` → live ComfyUI path.  Skipped by
  default; set `MANGA_AUTOPILOT_REAL_COMFY_E2E=1`,
  `MANGA_AUTOPILOT_COMFY_BASE_URL`, and
  `MANGA_AUTOPILOT_TEST_WORKFLOW_JSON` to enable.  Works with
  localhost, LAN, or cloud-GPU ComfyUI instances.
- **Webtoon + PDF autopilot export** — the autopilot export hook now
  generates a webtoon (full + per-page slices) and a PDF after page
  rendering.  `ManifestExports` includes `webtoon` (list of PNG
  paths) and `pdf` (path to `manga.pdf`).  E2E tests verify both
  outputs exist on disk and appear in the manifest.
- **Project re-edit E2E** — a generated project can be reopened from a
  new app instance, bubble text edited via `PATCH /bubbles/{id}`, and
  page PNGs / webtoon / PDF re-rendered and re-exported.  The
  `test_project_reedit_e2e.py` test verifies the full round-trip:
  generate → reopen → edit → re-render → re-export → manifest update.
  HTTP API-based dialogue editing and Web UI editing are planned but not
  yet wired up.
- **Autopilot failure-resume E2E** — when panel generation fails mid-run
  (e.g. executor error), the pipeline transitions to
  `FAILED_PANEL_GENERATION` and writes `generation_log.json`.  On resume
  (`POST .../autopilot/start` on the failed project), the idempotent
  `generate_panels` hook skips already-generated panels and generates
  only the missing ones.  The `test_autopilot_resume_e2e.py` test
  verifies the full fail → resume → complete round-trip with artefact
  checks.
- **Project bundle import E2E** — a generated project can be exported as
  a ZIP via `ExportService.zip()`, imported to a different `storage_root`
  via `ExportService.import_zip()`, and fully re-used from the new
  location.  The `test_project_bundle_import_e2e.py` test verifies:
  generate → ZIP export → import to different storage → new app instance
  fetches project → edit bubble text → re-render pages with updated text
  (hash change verified) → re-export webtoon/PDF → rebuild manifest →
  all artefacts intact with correct stats.

### Planned (spec-described, not yet wired up)

- **Web UI editing.**  Full in-browser editing of projects, panels,
  bubbles, and dialogue.  Currently bubble text can be edited via
  `PATCH /bubbles/{id}` HTTP API, but a visual editor is planned.
- **History-aware editing UI.**  Edit history with undo/redo and
  per-panel revision tracking.
- **Diff preview.**  Side-by-side before/after comparison when
  re-rendering after edits.
- **Complex panel layout AI.**  AI-driven panel composition that
  goes beyond the current grid/fallback layouts.
- **ZIP import conflict resolution UI.**  When importing a bundle that
  overlaps with an existing project, provide a UI to merge, overwrite,
  or skip conflicting files.
- **External GPU worker (Modal-style) end-to-end.**  `GPUBridge` knows
  how to serialise a workflow and fall back to local ComfyUI on
  timeout, but it is not yet used by the default orchestrator.
- **Real ComfyUI in normal CI.**  Making a live ComfyUI server
  required for standard CI runs (currently opt-in only).
- **Real-image QA scoring.**  Today the QA loop runs the heuristics
  (prompt alignment, bubble space, palette).  Real CLIP / IP-Adapter /
  face-similarity scoring is on the roadmap; the `GenerationLoop`
  already invokes the checkers and re-renders on failure, so dropping
  in a better scorer does not change the surrounding wiring.

See `docs/comfyui_manga_autopilot_spec.md` §30-§42 for the detailed
status of every phase.

### Opt-in Real ComfyUI E2E

The `test_real_comfy_executor_e2e.py` test is **skipped by default**
and only runs when three environment variables are set.  This keeps
the standard `pytest tests/backend/ -q` fast and GPU-free while
letting developers with a live ComfyUI server validate the full
executor path.

```bash
# Standard test suite (no GPU required):
pytest tests/backend/ -q

# Opt-in real ComfyUI E2E (requires a running ComfyUI server):
MANGA_AUTOPILOT_REAL_COMFY_E2E=1 \
MANGA_AUTOPILOT_COMFY_BASE_URL=http://192.168.1.50:8188 \
MANGA_AUTOPILOT_TEST_WORKFLOW_JSON=/path/to/workflow_api.json \
pytest tests/backend/test_real_comfy_executor_e2e.py -q
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `MANGA_AUTOPILOT_REAL_COMFY_E2E` | Yes | `0` | Set to `1` to enable the test |
| `MANGA_AUTOPILOT_COMFY_BASE_URL` | Yes | — | ComfyUI server URL (localhost, LAN, or cloud) |
| `MANGA_AUTOPILOT_TEST_WORKFLOW_JSON` | Yes | — | Path to a workflow JSON with `api_graph` + `bindings` |
| `MANGA_AUTOPILOT_REAL_COMFY_TIMEOUT` | No | `180` | Max seconds to wait for autopilot completion |

**Notes:**
- The workflow JSON must reference models/nodes that exist on the
  target ComfyUI server.
- Low-spec dev machines: run only the standard test suite — no GPU
  needed.
- GPU-equipped or remote machines: point `COMFY_BASE_URL` at the
  ComfyUI instance and run the opt-in test.

### v1.0 Release Gate

Before every release, run the following quality gate:

```bash
# Lint
ruff check .

# Full backend test suite
pytest tests/backend/ -q

# Release gate subset (key E2E only)
pytest -m release_gate -q
```

The `release_gate` marker targets the critical E2E paths:

| Test | What it covers |
|------|----------------|
| `test_one_page_autopilot_completes_end_to_end` | 1-page × 1-panel generation |
| `test_four_page_two_panel_autopilot_completes_end_to_end` | 4-page × 2-panel + webtoon/PDF |
| `test_one_page_autopilot_uses_comfy_executor_path` | ComfyExecutor + FakeComfyClient |
| `test_generated_project_can_be_reopened_edited_and_reexported` | Project re-edit round-trip |
| `test_failed_autopilot_can_resume_missing_panels_only` | Failure → resume → complete |
| `test_generated_project_bundle_can_be_imported_edited_and_reexported` | ZIP bundle import round-trip |

See [`docs/release/v1_acceptance_matrix.md`](docs/release/v1_acceptance_matrix.md)
for the full acceptance matrix.

---

## Features

- **Project + story planning** (LLM-driven, JSON-repair loop)
- **Character manager** — reference image upload, IP-Adapter, LoRA bindings
- **Workflow registry** — schema validation against live ComfyUI
  `object_info` and one-click test runs
- **Page / panel editor** — template-based layouts, composite image
  rendering, JSON persistence
- **Speech bubbles** — auto layout, vertical Japanese, PNG output,
  autopilot lettering hook with bubble overlay on rendered pages
- **Candidate generation** — multi-seed policy / **QA scoring** /
  retry prompts (spec §17-18)
- **Autopilot state machine** — pause / resume / cancel, recovery
  strategies (spec §7)
- **Export** — PNG pages, webtoon slices, PDF (A4/B5/Kindle/custom),
  zip-based project bundles
- **External GPU bridge** (Modal-style worker) — local ComfyUI fallback
  on timeout

## Requirements

- Python 3.10+
- ComfyUI 0.3.x+ (this custom node deals with API-format workflows)
- Pillow 10+
- `aiohttp` 3.9+
- `pydantic` 2+
- `jsonschema` 4+

## Install

See [`docs/install.md`](docs/install.md) for full details.

```bash
cd custom_nodes
git clone https://github.com/Kataage/ComfyUI-Manga-Autopilot
cd ComfyUI-Manga-Autopilot
pip install -e .
```

Restart ComfyUI; a "Manga Autopilot" sidebar tab appears in the UI.

## Local smoke test

After installing, verify the installation works:

```bash
python -m pip install -e .
ruff check .
pytest -m release_gate -q
```

## Quick start

See [`docs/quickstart.md`](docs/quickstart.md).

## Sample workflows

`workflows/` ships 5 ready-to-register API-format workflows:

- `anime_t2i_api.json`
- `anime_i2i_api.json`
- `anime_reference_api.json`
- `character_sheet_api.json`
- `upscale_api.json`

## Examples / Starter Kit

`examples/` contains ready-to-use workflow examples for new users:

- [`examples/workflows/anime_t2i_default.registry.json`](examples/workflows/anime_t2i_default.registry.json) —
  WorkflowRegistry payload (copy-paste into `/workflows` or use the API)
- [`examples/workflows/anime_t2i_default.workflow.json`](examples/workflows/anime_t2i_default.workflow.json) —
  ComfyUI API-format workflow graph
- [`examples/workflows/README.md`](examples/workflows/README.md) —
  step-by-step guide to adapt and register your own workflow

## Remote Executor Foundation

The remote executor path allows panel generation to be delegated to an
external HTTP GPU worker (e.g. Modal, RunPod, or a remote ComfyUI
server).  In this release:

- `RemoteHTTPExecutor` implements the `GenerationExecutor` protocol
- Typed exceptions: `RemoteExecutorHTTPError`, `RemoteExecutorTimeoutError`,
  `RemoteExecutorResponseError`, `RemoteExecutorImageError`
- When the remote worker fails, Autopilot reaches `FAILED` state and
  `generation_log.json` records the failure reason
- A `FakeRemoteWorker` in-process server is used for CI testing
- The real Modal / RunPod / external GPU integration is **not yet
  implemented**
- No external network service is required in standard CI

Set the executor via `manga_panel_executor` or
`manga_panel_executor_factory` on the aiohttp app.  See
`tests/backend/test_remote_executor_e2e.py` for a complete example.

## Documentation

- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`docs/release/v1_acceptance_matrix.md`](docs/release/v1_acceptance_matrix.md) —
  v1.0 acceptance matrix
- [`docs/release/v1_release_checklist.md`](docs/release/v1_release_checklist.md) —
  pre-release checklist
- [`docs/install.md`](docs/install.md) — install
- [`docs/quickstart.md`](docs/quickstart.md) — 6-step happy path
- [`docs/workflow_binding.md`](docs/workflow_binding.md) — workflow
  registry + binding model
- [`docs/character_consistency.md`](docs/character_consistency.md) —
  character consistency levels 1-7
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — common failure
  modes
- [`docs/contribution.md`](docs/contribution.md) — issue-first workflow
- [`docs/comfyui_manga_autopilot_spec.md`](docs/comfyui_manga_autopilot_spec.md) —
  authoritative spec

## License

This project is licensed under the terms of the [LICENSE](LICENSE) file.
The bundled sample workflows are distributed under the same licence, but
the model files they reference are not included in this repository.
