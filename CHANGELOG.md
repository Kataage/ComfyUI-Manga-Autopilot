# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

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
