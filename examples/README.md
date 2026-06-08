# Examples

This directory contains example configurations for Manga Autopilot.

## Contents

- **`workflows/`** — ComfyUI workflow and registry examples for use
  with the Autopilot pipeline and opt-in Real ComfyUI E2E tests.
- **`projects/`** — Generated project examples (placeholder; sample
  ZIPs will be added in a future release).

## Quick start

1. Copy `workflows/anime_t2i_default.registry.json` to your ComfyUI
   storage root under `workflows/`.
2. Replace the `api_graph` with your own exported workflow.
3. Update `bindings` node IDs to match your workflow.
4. Run the opt-in E2E test (see `workflows/README.md`).

## See also

- [Workflow examples README](workflows/README.md)
- [Projects examples README](projects/README.md)
