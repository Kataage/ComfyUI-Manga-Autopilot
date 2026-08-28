# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## Unreleased

Strict Anima support, and the defects that only appeared once the
pipeline was run against real hardware and loaded by ComfyUI itself.

### Added

- **Strict structured planning** — JSON Schema enforced on every planner
  response, with a repair loop and a hard failure when the shape is wrong.
- **Story bible and scene state reducer** — continuity carried between
  panels instead of re-derived per prompt.
- **Layout-aware panel planning** — panels planned against a layout
  catalogue and the running continuity state.
- **Anima generation profiles** — the profile, not the LLM, owns steps,
  CFG, seed and resolution; a prompt adapter renders them deterministically.
- **Project versioning and run snapshots** — lazy migration on read with a
  byte-identical backup, and a per-run `snapshot.json` carrying model
  SHA-256 fingerprints, full prompts, seeds and dimensions.
- **Strict Anima preflight** — endpoint and auth, licence acknowledgement,
  model and LoRA files, workflow bindings against the live node classes,
  references, resolution policy and output directory.
- **Review gates** — Story, Storyboard, Artwork Early and Artwork Final,
  with edit-driven invalidation. Image generation waits for Storyboard.
- **Managed LM Studio sessions** — only identifiers this process loaded are
  ever unloaded.
- **Planner cost measurement** — per-completion latency and reasoning
  volume, accumulated into `planner.reasoning_ratio` in the run snapshot,
  so a non-reasoning planner can be chosen on evidence.
- **CFG-1 warning** — ComfyUI skips the negative branch entirely at CFG 1,
  so the negative prompt has no effect; the profile is checked for it.
- **Positive-prompt negation linting** — negation in a positive prompt
  renders the thing it names.
- **`requirements.txt`** — ComfyUI and ComfyUI-Manager install this file and
  do not read `pyproject.toml`; without it `jsonschema` was missing and every
  HTTP route failed to register while the sidebar tab still rendered.

### Fixed

- Application-level negative bans now reach the sampler; the submit path
  used `prompt.negative` and dropped them.
- The run snapshot is actually written, and models resolve through
  `extra_model_paths.yaml`.
- Strict Anima prompts are rendered by the profile adapter in the route,
  not by the older LLM-driven builder.
- Panel planning asks which characters appear in each panel, so a
  character's identity reaches the prompt.
- Silent panels stay silent — no invented dialogue in strict runs.
- Bubble text is drawn with a real font, sized to fit the bubble.
- Page and panel planning failures fail the run instead of continuing.
- An unreachable ComfyUI is named, instead of surfacing a bare
  `FAILED_PANEL_GENERATION`.
- OpenAI-compatible endpoint errors are surfaced instead of an empty
  completion; the provider has a timeout that says so when it expires.
- A whole sentence is no longer read as a character's hair or eye colour.
- A decided review's accessible label keeps the gate name; the note alone
  used to replace it for screen readers.
- The ComfyUI sidebar workspace is usable: the Projects tab is no longer
  blocked by the guard only it can clear, and the workspace fits a 312px
  panel that clips overflow.
- Preflight runs the six checks that need no `/object_info` when no ComfyUI
  client is configured. Stepping aside entirely used to drop the licence
  acknowledgement and the remote-endpoint auth check as well.
- The autopilot start route seeds its run input from the project, so the
  `generation_profile_id` and `license_acknowledged` set through
  `PATCH /projects/{id}` reach strict mode and preflight. They previously
  reached only the review gates, which made strict mode look enabled when
  it was off.

### Changed

- `AnimaPreflight.capabilities` accepts `None`. A missing ComfyUI client is
  not a failure: `manga_remote_executor` is a supported deployment with no
  local ComfyUI to interrogate. The report carries
  `comfy.capabilities_unavailable` to record what could not be checked.

### Known limitations

- `page_count` reaches a run only through the autopilot start body. The
  restart route restores it from `project.json`; start does not, so a
  project started as `docs/anima_mvp.md` describes - with no body - plans
  against `page_count` 1 and any multi-page plan fails validation. Aligning
  them would also adopt the project's `candidate_count` of 4 against
  start's 1, which changes generation cost.
- Establishing shots carry no character anchor by design, and warn.
- Turbo suppression has no scene-independent positive phrasing yet.

## 0.1.0-rc1

First release candidate.  Covers the full v1.0 spec pipeline from idea
to exported manga / Webtoon.

### Added

- **Autopilot pipeline** — state machine with 16 happy-path + 8 failure
  states, pause / resume / cancel, error recovery table.
- **Story / character / page / panel planning** — LLM-driven with
  JSON-repair loop; character consistency levels 1-7.
- **Multi-page / multi-panel generation** — tested with 1p1c, 2p1c,
  4p1c, 1p2c, 1p3c, 4p2c configurations.
- **Speech bubble generation and overlay** — auto layout, vertical
  Japanese, PNG rendering, autopilot lettering hook.
- **Page PNG export** — composites panel borders + generated images
  into page PNGs (cover / contain / stretch fit modes).
- **Webtoon export** — vertical stitching + height-based slicing.
- **PDF export** — A4 / B5 / Kindle / custom sizes, configurable
  margins and DPI.
- **ComfyExecutor path** — real `ComfyExecutor` + `FakeComfyClient`
  E2E without a live ComfyUI server.
- **Real ComfyUI opt-in E2E** — environment-variable-gated smoke
  test against a live ComfyUI server.
- **Project re-edit E2E** — reopen project, edit bubble text via
  `PATCH /bubbles/{id}`, re-render pages, re-export webtoon / PDF.
- **Failure resume E2E** — mid-run executor failure transitions to
  `FAILED_PANEL_GENERATION`; resume skips generated panels and
  completes only the missing ones.
- **ZIP bundle export / import E2E** — export project as ZIP, import
  to a different `storage_root`, edit, re-render, re-export.
- **HTTP API** — full REST surface for projects, panels, bubbles,
  characters, workflows, autopilot control, and exports.
- **Web extension** — sidebar tab, project picker, page editor,
  character manager, progress monitor, export center.
- **v1.0 release gate** — `release_gate` pytest marker, acceptance
  matrix document, CI workflow.

### Fixed

- `generation_log.json` is now always written, even when the Autopilot
  pipeline fails (previously an unhandled `InvalidTransitionError`
  could skip `_finalize()`).
- `max_retries=0` is now respected; the `or 1` default previously
  treated `0` as falsy and silently defaulted to `1`.

### Known limitations

- Web UI full editor is not implemented (bubble text can be edited
  via HTTP API only).
- External GPU worker (`GPUBridge`) is not wired as the default
  executor path.
- Real CLIP / IP-Adapter / face-similarity QA scoring is not
  implemented.
- Complex panel layout AI (beyond grid / fallback) is not implemented.
- ZIP import conflict resolution UI is not implemented.
- Real ComfyUI is opt-in and not required in standard CI.
