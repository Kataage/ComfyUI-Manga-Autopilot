# Claude Code handoff: Anima Manga Autopilot MVP

Updated: 2026-08-26 JST (Claude Code, Task 5 complete)

## Objective

Continue implementing the Anima MVP described in:

`C:\Users\kouda\Downloads\codex_manga_autopilot_anima_requirements_v1.0.md`

The user accepted the recommended choices from the prior grilling session and asked that the remaining choices also use the recommendations. The agreed implementation plan is:

`docs/superpowers/plans/2026-08-26-anima-mvp.md`

Follow that plan from Task 6 onward. Do not restart completed Tasks 1-5.

## Repository and Git state

- Repository: `C:\Users\kouda\Documents\Codex\2026-08-26\new-chat\work\ComfyUI-Manga-Autopilot`
- Branch: `codex/anima-mvp`
- Base: `c88a542` (`origin/main` at clone time)
- Python environment: `.venv`
- Workspace-local pytest temp: `..\test-tmp`

Completed commits:

1. `8a95977 feat: enforce strict structured planning`
2. `a251243 feat: add story bible and scene state reducer`
3. `eb52846 feat: plan panels against layouts and continuity`
4. `968399e feat: add Anima generation profiles and prompt adapter`

5. `<pending> feat: version projects and snapshot Anima runs`

See "Task 4 result" and "Task 5 result" below for what those commits contain.

## Completed behavior

### Task 1

- LM Studio/OpenAI-compatible calls send strict JSON Schema response format.
- JSON Schema validation covers the complete supplied schema.
- One repair attempt can include semantic errors and prior response history.
- Strict story/page/panel planning checks counts, ordering, character IDs, and layouts.
- Generic planner behavior remains opt-in and backward compatible.

### Task 2

- Added Story Bible models.
- Added persisted Scene State, event delta, warnings, and atomic pure reducer.
- Unknown characters and impossible ownership block the complete delta.
- Unexplained clothing changes produce warnings while applying the valid delta.
- Added `story_bible.json` and `scene_state.json` project paths.

### Task 3

- Added registered layout catalog with slot counts and Japanese page reading order.
- Added deterministic `fallback_grid_N` layouts.
- Strict Anima planning reuses the page plans produced by the global Story Planner and makes no second page-planning LLM call.
- Panel prompts receive Story Bible, prior Scene State, character records, and registered layouts.
- Scene deltas are reduced and persisted sequentially after every panel.
- Strict Anima errors propagate; generic projects retain the prior stub fallbacks.

Latest Task 3 verification:

```powershell
$env:TEMP=(Resolve-Path ..\test-tmp).Path
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m pytest tests/backend/test_layout_contract.py tests/backend/test_continuity_planning.py tests/backend/test_page_templates.py tests/backend/test_story_planner.py tests/backend/test_page_planner.py tests/backend/test_panel_planner.py tests/backend/test_one_page_e2e.py -q
```

Result: `41 passed, 6 warnings`.

Targeted Ruff result: all checks passed. `git diff --check` also passed before the Task 3 commit.

## Task 4 result

Committed as `968399e`. Delivered files:

- `src/manga_autopilot/models/generation_profile.py` - `LicenseMetadata`, `GenerationSettings`, `ModelAssets`, `ResolutionPolicy`, `ResolvedResolution`, `GenerationProfile`, `SemanticPromptSegments`, plus `POSITIVE_SEGMENT_ORDER` and `PROFILE_OWNED_FIELDS`.
- `src/manga_autopilot/services/generation_profiles.py` - `load_builtin_profile`, `list_builtin_profiles`, `resolve_panel_resolution`, `resolve_default_resolution`.
- `src/manga_autopilot/services/anima_prompt_builder.py` - `AnimaPromptBuilder.render(segments, profile, *, seed, panel_size=None) -> PromptSpec`.
- `src/manga_autopilot/profiles/__init__.py`, `anima_base.json`, `anima_aesthetic.json`, `anima_turbo.json`.
- `pyproject.toml` - `[tool.setuptools.package-data]` ships `manga_autopilot.profiles/*.json`.
- `src/manga_autopilot/models/__init__.py` - re-exports the new models.
- Tests: `tests/backend/test_generation_profiles.py` (15 tests), `tests/backend/test_anima_prompt_builder.py` (11 tests).

`src/manga_autopilot/services/prompt_builder.py` was deliberately left unchanged. `PromptSpec` and its `negative_full()` bans are reused as they are, so generic projects keep the previous LLM-driven behavior.

Design decisions worth knowing:

- Resolution is a pure function of the panel aspect ratio. The policy keeps a 1,228,800-pixel budget, rounds each side to the nearest multiple of 64, then clamps to 512-1536. Extreme ratios clamp rather than exceed the supported side range: 8:1 resolves to 1536x512.
- `SemanticPromptSegments.technical_overrides` is accepted but never applied. `AnimaPromptBuilder` logs a warning naming the ignored keys. Steps, CFG, sampler, scheduler, and dimensions come from the profile; the seed comes from the caller.
- Positive order: profile `quality_prefix`, `identity`, `must_keep`, `subject`, `action`, `camera`, `emotion`, `background`, `lighting`, `style` (segment style first, then `style_defaults`). Terms are comma-split, trimmed, and deduplicated case-insensitively keeping the first occurrence.
- `anima_aesthetic` sets `allow_score_tags: false`, which strips `score_*` from both prompts per the model card.
- Profile JSON is loaded through `importlib.resources` and cached with `functools.cache`. Unknown IDs raise `KeyError`.
- `anima_base` uses steps 30 / CFG 4.5 and `anima_aesthetic` uses steps 32 / CFG 4.0, both inside the model card's 30-50 step and CFG 4-5 guidance. Only `anima_turbo` mirrors the verified local workflow exactly.

Verification (2026-08-26, Claude Code):

```powershell
$env:TEMP='<a temp dir the agent can write to>'
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m pytest tests/backend/test_generation_profiles.py tests/backend/test_anima_prompt_builder.py tests/backend/test_prompt_builder.py -q -p no:cacheprovider
```

Result: `33 passed in 0.46s`.

Full backend suite: `7 failed, 858 passed, 15 skipped` in 68s. The 7 failures are the pre-existing portability issues listed under "Baseline suite caveat".

`.\.venv\Scripts\python.exe -m ruff check src/manga_autopilot tests/backend/test_generation_profiles.py tests/backend/test_anima_prompt_builder.py` reported `All checks passed!`, and `git diff --check` was clean.

Environment note: `..\test-tmp` is not writable from every agent sandbox. If pytest reports `PermissionError ... pytest-of-kouda`, point `$env:TEMP` and `$env:TMP` at a temp directory the current agent can write to. Pass `-p no:cacheprovider` when `.pytest_cache` is also unwritable.

## Task 5 result

Delivered files:

- `src/manga_autopilot/services/project_migration.py` - `detect_schema_version`, `migrate_document`, `migrate_project_document`, `backup_project_document`, `write_project_document`, `UnsupportedSchemaVersionError`.
- `src/manga_autopilot/services/model_fingerprint.py` - `ModelFingerprint`, `FingerprintCache`, `sha256_file`.
- `src/manga_autopilot/services/run_snapshot.py` - `RunSnapshot`, `RunSnapshotWriter`, `PanelPromptSnapshot`, `LLMSettingsSnapshot`, `EnvironmentSnapshot`, `hash_json_document`, `scrub_secrets`, `assert_no_secrets`, `log_prompt_digest`.
- `src/manga_autopilot/models/project.py` - `CURRENT_PROJECT_SCHEMA_VERSION = 2`, `MigrationRecord`, and the `schema_version` / `migration_history` fields.
- `src/manga_autopilot/storage/paths.py` - `ProjectPaths.backups`.
- `src/manga_autopilot/services/project_manager.py` - migrating load, backup-then-atomic-write save.
- `src/manga_autopilot/services/run_artifacts.py` - `snapshot.json` reported in the run artifact summary.
- `src/manga_autopilot/services/export.py` - `ProjectBundler.bundle(..., include_sources=False)` and `ExportService.zip(..., include_sources=False)`.
- Tests: `tests/backend/test_project_migration.py` (12), `tests/backend/test_run_snapshot.py` (19), plus 3 in `test_run_artifacts.py` and 3 in `test_export.py`.

Design decisions worth knowing:

- Migration is lazy. `migrate_project_document` reads and upgrades in memory and never writes; `ProjectManager.load` uses it, so opening an old project leaves the file byte-identical. The backup and rewrite happen on the first save, and `backups/project.json.<utc-stamp>.bak` is a byte-for-byte copy of the original.
- The on-disk document, not the in-memory `Project`, owns `schema_version` and `migration_history`. `_write` merges the model dump onto the existing document, so fields this build does not model (and the migration audit trail) survive a save by a stale caller.
- Writes go through a temporary sibling plus `Path.replace`, so an interrupted save cannot truncate `project.json`.
- v1 to v2 only stamps the version and records the migration. Asset paths and every other field are deliberately untouched.
- Fingerprints keep the model's name, size, and SHA-256 but never its absolute path, so a snapshot does not leak the user's directory layout. The cache is keyed by resolved path and invalidated by size or mtime.
- Secret detection matches whole normalised key names and singular suffixes (`_key`, `_token`, ...), not substrings. `max_tokens` is a length budget and is kept; `access_token` is a credential and is dropped. `RunSnapshotWriter.write` refuses to serialise a document that still carries one.
- The output-only bundle is an allowlist: `exports/` plus `manifest.json`. It cannot leak a prompt even if a future release starts writing prompts into a new project-root file.

Verification (2026-08-26, Claude Code):

```powershell
$env:TEMP='<a temp dir the agent can write to>'
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m pytest tests/backend/test_project_migration.py tests/backend/test_run_snapshot.py tests/backend/test_project_manager.py tests/backend/test_run_artifacts.py tests/backend/test_export.py -q -p no:cacheprovider
```

Result: `76 passed`.

Full backend suite: `7 failed, 895 passed, 15 skipped` in 66s - the same 7 pre-existing portability failures, no regressions.

`.\.venv\Scripts\python.exe -m ruff check src/manga_autopilot tests/backend` reported `All checks passed!`.

## Verified Anima configuration evidence

The read-only local workflow was checked on 2026-08-26:

- Source workflow: `C:\Users\kouda\OneDrive\ドキュメント\Claude\Projects\me fine you\anima_two_prompt\workflow\anima_two_prompt_v1.json`
- Installed workflow: `C:\Users\kouda\AppData\Local\Comfy-Desktop\Data\Packages\ComfyUI\user\default\workflows\anima_two_prompt_v1.json`
- Both SHA-256: `161775C7EDE90789A1D26BEFDBCB5A591EBDE19D16D2B54BF80CF15B994A4B49`
- Model: `silvermoonmixAnima_v23.safetensors`
- Turbo LoRA: `anima-turbo-lora-v0.2.safetensors`
- Text encoder: `qwen_3_06b_base.safetensors`
- VAE: `qwen_image_vae.safetensors`
- Sampler/scheduler: `er_sde` / `simple`
- Steps/CFG: `12` / `1`
- Portrait resolution: `960x1280`
- Candidate count: `1`
- Technical retry count: `1`
- Quality retry count: `0`

The official Anima model card was checked during Task 4 preparation:

- `https://huggingface.co/circlestone-labs/Anima`
- Base/general guidance: 512-square through 1536-square range, 30-50 steps, CFG 4-5; `er_sde` is described as a reasonable default.
- Turbo guidance: CFG 1 and 8-12 steps.
- Recommended general prefix: `masterpiece, best quality, score_7, safe`.
- Aesthetic guidance: omit `score_*` tags; `masterpiece, best quality` is acceptable.
- License: CircleStone Labs Non-Commercial License, with additional NVIDIA derivative-model terms noted by the model card. Model weights and LoRAs must not be bundled or downloaded automatically.

For deterministic resolution tests, the accepted expectations are:

- portrait 3:4 -> `960x1280`
- landscape 4:3 -> `1280x960`
- square 1:1 -> `1088x1088`
- all dimensions are multiples of 64 and within 512-1536

## Live environment snapshot

Read-only checks from 2026-08-26:

- ComfyUI endpoint: `http://127.0.0.1:8188`
- ComfyUI version: `0.30.0`
- Python: `3.12.10`
- PyTorch: `2.13.0+cu130`
- GPU: RTX 5070 Ti
- Queue was `0/0`
- `/object_info` exposed 1910 nodes
- Required workflow node types were present: `UNETLoader`, `CLIPLoader`, `VAELoader`, `ResolutionSelector`, `LoraLoader|pysssss`, `CLIPTextEncode`, `EmptyLatentImage`, `KSampler`, `VAEDecode`, `SaveImage`
- `lms ps --json` returned `[]`; no LM Studio model was loaded

Do not launch a GPU generation, download/load a model, interrupt ComfyUI, or alter the installed workflow without explicit user approval. Normal implementation and tests must remain GPU-free.

## Baseline suite caveat

After creating `.venv` and installing dev dependencies, the baseline backend suite produced `799 passed, 11 failed, 15 skipped`. The Task 4 verification run produces `858 passed, 7 failed, 15 skipped`; the earlier baseline was measured with a partly unwritable temp directory, so its failure count was inflated.

The 7 remaining failures predate this branch and are Windows/sandbox portability issues tracked for Task 8:

- `test_artifact_store.py::TestFactoryFromEnv::test_local_store_uses_env_root` - a hard-coded Unix `/tmp` expectation resolves to `C:/tmp/...`
- `test_character_service.py::test_register_reference_image` - asserts a forward-slash asset path against the persisted `assets\characters\alice\ref_001.png`
- `test_release_readiness.py` (5 tests) - README reads use the Windows default code page (`cp932`) instead of explicit UTF-8

Task 8 should fix the tests or test fixtures with `tmp_path`, normalized persisted asset references, and explicit UTF-8 reads. Do not weaken the corresponding production behavior.

## Remaining sequence

1. Implement Task 6 strict preflight, sequential generation semantics, and managed LM Studio lifecycle. Keep all live work opt-in.
2. Implement Task 7 review gates, edit invalidation, and fake-service E2E.
3. Implement Task 8 form review UI, API-format workflow example, docs, portability fixes, and full non-GPU verification.
4. Run the final verification commands in the plan and inspect `git status`, `git diff --check`, and commit history.

## Standing approvals (2026-08-26)

Granted by the user in the Claude Code session that implemented Tasks 4 and 5:

- Commit a task yourself once its tests are green, `ruff check` passes, and the full backend suite shows no new failures. Do not ask per task.
- Still ask before: `git push`, adding a dependency, downloading or loading a model, running anything on the GPU, and any use of an external service.

## Implementation constraints and decisions

- Extend the upstream subsystems; do not replace them.
- Gate strict behavior by `generation_profile_id` beginning with `anima_`.
- Preserve generic project loading and generation behavior.
- New Anima runs use Story, Storyboard, and Artwork review gates.
- Image generation must wait for Storyboard approval.
- Store complete rendered prompts in per-run snapshots, but diagnostic logs should contain prompt hashes rather than prompt text.
- Keep credentials out of snapshots and logs.
- Persist a completed panel before submitting the next panel.
- Resume should skip completed panels.
- A quality rejection should wait for user action; only a technical failure receives one automatic retry.
- No invented dialogue fallback in strict Anima lettering.
- Project migration should be lazy on read and create a byte-identical timestamped backup before the first migrated save.
- Output-only export excludes source prompts; backup export includes reproducibility material.
- Use Qwen3.5-9B for later live acceptance if the user approves loading it. Do not auto-download it.

## Suggested first command in Claude Code

```powershell
Set-Location 'C:\Users\kouda\Documents\Codex\2026-08-26\new-chat\work\ComfyUI-Manga-Autopilot'
git status --short
git log --oneline -4
Get-Content -LiteralPath HANDOFF.md -Encoding utf8
```
