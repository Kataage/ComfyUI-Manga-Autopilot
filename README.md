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
  orchestrator in a background `asyncio.Task`; the orchestrator
  cooperatively `await`s an `asyncio.Event` on each step, so `pause`
  is honoured mid-step and `resume` restores the pipeline to the
  state it was in before the pause.
- **Web extension** — sidebar tab, project picker, page editor,
  character manager, progress monitor, export center, all mounted from
  `web/index.js`.

### Planned (spec-described, not yet wired up)

- **LLM-driven story / character / page / panel planners.**  Today the
  default orchestrator hooks call the corresponding services with
  empty / placeholder inputs.  The planner modules themselves are
  implemented and unit-tested, but the default project bootstrap does
  not yet feed them with an LLM response — that wiring is tracked
  separately.
- **External GPU worker (Modal-style) end-to-end.**  `GPUBridge` knows
  how to serialise a workflow and fall back to local ComfyUI on
  timeout, but it is not yet used by the default orchestrator.
- **Image-quality / prompt-alignment / character-consistency checkers.**
  The QA + retry service has the hooks, but the per-checker scoring
  defaults to a constant.  Real CLIP / IP-Adapter / face-similarity
  scoring is on the roadmap.

See `docs/comfyui_manga_autopilot_spec.md` §30-§42 for the detailed
status of every phase.

---

## Features

- **Project + story planning** (LLM-driven, JSON-repair loop)
- **Character manager** — reference image upload, IP-Adapter, LoRA bindings
- **Workflow registry** — schema validation against live ComfyUI
  `object_info` and one-click test runs
- **Page / panel editor** — template-based layouts, composite image
  rendering, JSON persistence
- **Speech bubbles** — auto layout, vertical Japanese, PNG output
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

## Quick start

See [`docs/quickstart.md`](docs/quickstart.md).

## Sample workflows

`workflows/` ships 5 ready-to-register API-format workflows:

- `anime_t2i_api.json`
- `anime_i2i_api.json`
- `anime_reference_api.json`
- `character_sheet_api.json`
- `upscale_api.json`

## Documentation

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
