# Troubleshooting

Common failure modes and how to fix them.

## Routes do not appear

1. Check that the custom node is on `sys.path` inside ComfyUI
   (re-run `pip install -e .`).
2. Confirm `WEB_DIRECTORY` resolves to this repo's `web/` directory.
3. Look for `manga_autopilot` errors in the ComfyUI console.

## Health check fails

```bash
curl http://localhost:8188/manga_autopilot/api/health
```

A non-200 response means the custom node failed to attach routes. The
ComfyUI console will show the underlying `aiohttp` traceback.

## LLM-driven planners return 400

The repair loop raises a `ValueError` only after the configurable number
of repair attempts is exhausted. The HTTP response is the JSON
serialization of the underlying error. Increase
`max_repair_attempts` on the planner, or switch to a smaller /
`manual` model and edit the JSON by hand.

## Panels fail QA

The QA pipeline uses the weighted total from spec section 18.4. A panel
that scores below `quality_threshold` is sent to `RetryController` which
emits prompt revisions and may switch seed / workflow. Inspect
`{storage_root}/projects/{id}/qa_report.json` for the per-check scores
and `generation_log.json` for the retry history.

## External GPU worker unreachable

`GPUBridge` records the failure on the `ExternalGPUClient` and asks
`GPUFallbackPolicy` whether to fall back to the local ComfyUI server.
Set `GPUFallbackPolicy.enabled = False` to fail loudly instead.

## Storage paths

The on-disk layout is documented in
`src/manga_autopilot/storage/paths.py`. Use the helpers
`ensure_storage_root` / `ensure_project_paths` rather than constructing
paths by hand so that tests and the ComfyUI integration agree.

## Tests

```bash
pytest tests/backend/         # 400+ tests
ruff check .                  # style + import order
```

## Still stuck?

Open an issue at
<https://github.com/Kataage/ComfyUI-Manga-Autopilot/issues>. Include
the ComfyUI console output and, where relevant, the contents of
`generation_log.json` / `qa_report.json`.
