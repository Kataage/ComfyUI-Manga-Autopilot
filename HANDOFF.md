# Claude Code handoff: Anima Manga Autopilot MVP

Updated: 2026-08-31 JST (Codex; plan complete, proven end to end on
hardware including a real LM Studio planner and real ComfyUI rendering
inside a live install, not just the in-process test wiring)

## Objective

Continue implementing the Anima MVP described in:

`C:\Users\kouda\Downloads\codex_manga_autopilot_anima_requirements_v1.0.md`

The user accepted the recommended choices from the prior grilling session and asked that the remaining choices also use the recommendations. The agreed implementation plan is:

`docs/superpowers/plans/2026-08-26-anima-mvp.md`

Every task in that plan is done, and the pipeline has been proven end to end on
real hardware: a two-page manga planned by a real LLM, rendered on the GPU,
driven through all four review gates over the HTTP API, and exported as pages, a
webtoon strip and a PDF.

**Start here on resuming.** Read "Start of the next session" immediately below,
then "What is not done". Nothing else in this file is needed to pick up work.

## Start of the next session

Current state, rechecked on 2026-08-31:

- Branch `codex/anima-mvp` tracks `fork/codex/anima-mvp`. Confirm whether it is
  ahead or behind with `git rev-list --left-right --count fork/codex/anima-mvp...HEAD`.
- `1121 passed, 15 skipped`; `ruff check .` clean; `git diff --check` clean.
- The Windows test-portability correction described in the 2026-08-31 session
  boundary uses pytest's `tmp_path` instead of hard-coded POSIX `/tmp`.
- No `TODO`/`FIXME` in `src/` or `web/`.
- PR https://github.com/Kataage/ComfyUI-Manga-Autopilot/pull/219 is open against
  upstream `main`; compare its fork branch with `fork/codex/anima-mvp` before
  assuming its commit set.

Verify it still holds before changing anything:

```powershell
cd 'C:\Claude Code\comfyui-manga-autopilot'
git status --short
git rev-list --count fork/codex/anima-mvp..HEAD
$env:TEMP='<a temp dir you can write to>'; $env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m pytest tests/backend -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check .
```

The `.venv` works at this path; do not move it. Point `TEMP`/`TMP` at a
directory the running agent can write to, or `tmp_path` fixtures fail with
`PermissionError`.

Live runs need ComfyUI on `127.0.0.1:8188` and LM Studio on `127.0.0.1:1234`.
There are two ComfyUI installs on this machine; the running one is the Stability
Matrix build. Check `lms ps --json` first: if a model the user loaded is resident,
leave it alone and wait rather than evicting it.

Then read "What is not done". The dated "Session boundary" entries below are
history - useful for why something was done, not needed to resume.

## Session boundary (2026-08-31)

- **Worker / purpose:** Codex resumed the Task 4/MVP status check and ran the
  non-GPU backend regression suite.
- **Changed file:** `tests/backend/test_export.py`. Its two `WebtoonSlicer`
  tests no longer hard-code POSIX `/tmp`; they use pytest's isolated `tmp_path`
  fixture. On Windows, `/tmp` resolves to `C:\tmp` and caused a sandbox-host
  `PermissionError`, unrelated to the slicer implementation.
- **Validation:** focused export suite `22 passed`; backend suite `1121 passed,
  15 skipped` in 78.36s; `ruff check .` passed; `git diff --check` passed.
- **Unresolved scope:** the only skipped checks are opt-in real ComfyUI, Modal,
  and S3/R2 E2E. Do not run them, load a model, or use GPU/external services
  without fresh explicit approval.
- **Git:** this correction is intentionally uncommitted; no commit or push was
  requested. The next safe action is a diff review, then request commit/push
  direction separately if the user wants to publish it.

## Session boundary (2026-08-30)

Resumed exactly where 2026-08-29 stopped ("before running the live test") and
ran it. It failed twice on infrastructure that turned out to have never been
wired for a live install (see "`config.yaml` had no effect..." and "A second
instance, found by actually running it." below); both fixed, committed, and
confirmed by a third run that completed end to end - see "Third live run:
completed end to end (2026-08-30)".

The branch moved ahead of `fork/codex/anima-mvp` without being pushed:
2 fixes (`2fcfc52`, `0d7848c`) and the docs commits recording them. See
"Start of the next session" for the current count rather than trusting a
number written here. Local
suite and `ruff check .` both green after each (see those sections for
counts). Whether/when to push and update PR #219 is the user's call, per
the standing approvals below.

## Session boundary (2026-08-29 09:xx)

Stopped before running the live test - superseded by the entry above.

**Pushed to a fork, PR open** (as of 2026-08-29; the 4 commits above are not
included yet). `koudai715-code` has only `READ` on
  `Kataage/ComfyUI-Manga-Autopilot`, so the branch lives on the fork
  `koudai715-code/ComfyUI-Manga-Autopilot` (remote `fork`, which the branch
  now tracks) and
  https://github.com/Kataage/ComfyUI-Manga-Autopilot/pull/219 carries all 48
  commits into `main`. `origin` still points at the upstream. Working tree
  clean.

  A correction, because this file got it wrong in both directions on
  2026-08-28: a `refs/remotes/origin/codex/anima-mvp` ref existed at
  `fd269e8` and looked like 14 pushed commits. It was a leftover from the
  *old* origin - the remote was repointed from a local path to GitHub during
  the 2026-08-27 rescue, and the stale tracking ref survived the change.
  `git remote prune origin` removed it. Never conclude what a remote holds
  from a fetched ref; ask the remote.
## Repository and Git state

- Repository: `C:\Claude Code\comfyui-manga-autopilot` (relocated from the Codex sandbox on 2026-08-27; the `.venv` works at the new path)
- Branch: `codex/anima-mvp`
- Base: `c88a542` (`origin/main` at clone time)
- Python environment: `.venv`
- Workspace-local pytest temp: `..\test-tmp`

Completed commits:

1. `8a95977 feat: enforce strict structured planning`
2. `a251243 feat: add story bible and scene state reducer`
3. `eb52846 feat: plan panels against layouts and continuity`
4. `968399e feat: add Anima generation profiles and prompt adapter`
5. `a04cb48 feat: version projects and snapshot Anima runs`
6. `abd80f1 feat: add strict Anima preflight and managed execution`
7. `39568b8 feat: gate and invalidate Anima projects safely`
8. `3f72973 docs: ship Anima MVP review workflow`

See the per-task result sections below for what those commits contain.

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

## Task 6 result

Delivered files:

- `src/manga_autopilot/services/preflight.py` - `AnimaPreflight`, `PreflightRequest`, `PreflightReport`, `PreflightIssue`, `ComfyCapabilities`, `PreflightError`, `is_loopback`.
- `src/manga_autopilot/services/lm_studio_lifecycle.py` - `ManagedLMStudioSession`, `LoadedModel`, `run_lms_cli`, `redact_secrets`.
- `src/manga_autopilot/services/generation_job.py` - `ExecutorTimeout`, strict `GenerationLoopConfig` flags, `GenerationLoop._submit`/`_reconcile`, `run_panels_sequentially`, `SequentialPanelResult`.
- `src/manga_autopilot/models/job.py` - `JobStatus.AWAITING_REVIEW`.
- `src/manga_autopilot/config.py` - `ComfyUISettings.auth_token_env` plus `auth_is_configured()`, and `LMStudioSettings`.
- `src/manga_autopilot/routes/autopilot_routes.py` - preflight gate, profile-derived candidate/retry counts, strict runs routed through `run_panels_sequentially`.
- Tests: `test_anima_preflight.py` (23), `test_sequential_generation.py` (14), `test_lm_studio_lifecycle.py` (16), plus an updated `JobStatus` assertion in `test_generation_job.py`.

Live interfaces verified on 2026-08-26 before writing any of this (read-only):

- ComfyUI `/object_info`: combo inputs arrive as `[[option, ...], {config}]`, typed inputs as `["MODEL"]`. The three model files and the LoRA that `anima_turbo` declares are all installed on this machine.
- LM Studio CLI: `lms load <model-key> [--identifier X] [--ttl N] [--context-length N] [--gpu R] -y`, `lms unload <identifier>` (and a `--all` that this project must never issue), `lms ps --json` returning objects with `identifier`, `modelKey`, `status`.
- LM Studio had the user's own `gemma-4-26b-a4b-it` loaded at the time, which is exactly the instance the session must not touch.

Design decisions worth knowing:

- Preflight is pure. It takes a `/object_info` snapshot the caller already fetched, so the whole check runs without a server, and it never writes anything except a probe file it deletes.
- Warnings do not block. `report.ok` is false only for errors; `raise_if_blocked()` names every failing code.
- The plan's "unsupported dimensions" check became two honest checks: `resolution.policy_invalid` (error) for a self-inconsistent policy, and `resolution.aspect_clamped` (warning) when a panel aspect cannot be honoured inside the side limits. A check that the resolver's own output is in range would be vacuous, since the resolver guarantees it.
- Strict mode is a flag on `GenerationLoopConfig`, not a fork of the loop. Generic projects keep the existing candidate/retry/fallback behaviour untouched.
- A strict quality rejection sets `AWAITING_REVIEW` and stops. A technical failure retries `technical_retry_count` times (1 from the profile). A timeout carrying a prompt id reconciles through `executor.reconcile(prompt_id)` first, so a render that actually finished is adopted instead of paid for twice.
- A cancelled panel keeps its previous record status rather than being marked failed, so the run stays resumable.
- `ManagedLMStudioSession` adopts an instance the user already had loaded and records `owns_instance=False`; `unload()` is then a no-op. Bulk (`--all`) and download (`get`) commands are rejected inside `_invoke`, not merely avoided.

Known gap, deliberate:

- The route's preflight gate steps aside with a warning when the application has no `manga_comfy_client` or `manga_workflow_registry`. The in-process test wiring uses fake executors and has neither, and failing a run the gate cannot actually assess would be worse than logging. In a real install both are present and the gate is live. Task 7 or 8 should decide whether a strict run must hard-fail when it cannot preflight.

Verification (2026-08-26, Claude Code):

```powershell
$env:TEMP='<a temp dir the agent can write to>'
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m pytest tests/backend/test_anima_preflight.py tests/backend/test_sequential_generation.py tests/backend/test_lm_studio_lifecycle.py tests/backend/test_generation_job.py tests/backend/test_comfy_executor_e2e.py -q -p no:cacheprovider
```

Result: `69 passed`.

Full backend suite: `7 failed, 948 passed, 15 skipped` in 65s - the same 7 pre-existing portability failures, no regressions.

`.\.venv\Scripts\python.exe -m ruff check src/manga_autopilot tests/backend` reported `All checks passed!`.

## Task 7 result

Delivered files:

- `src/manga_autopilot/services/review_gate.py` - `ReviewPolicy`, `ReviewBoard`, `ReviewGateState`, `ReviewDecision`, `ReviewStore`, `ReviewCoordinator`, `split_for_early_review`, `run_with_early_artwork_review`.
- `src/manga_autopilot/services/edit_invalidation.py` - `EditDescriptor`, `InvalidationResult`, `compute_invalidation`, `apply_invalidation`.
- `src/manga_autopilot/routes/review_routes.py` - `GET /reviews`, `POST /reviews/{gate}/approve`, `POST /reviews/{gate}/reject`, plus coordinator registration.
- `src/manga_autopilot/services/autopilot.py` - `AutopilotRun.awaiting_review`, `Orchestrator.reviews`, gate waits after Story, before generation, and before lettering; `start_orchestrator(reviews=...)`.
- `src/manga_autopilot/routes/autopilot_routes.py` - strict runs render page one, wait for the early artwork review, then the rest; the coordinator is published for the review API at every start/restart site.
- `src/manga_autopilot/routes/project_routes.py` - `generation_profile_id` and `license_acknowledged` are patchable.
- `src/manga_autopilot/models/project.py` - those two fields.
- `src/manga_autopilot/services/generation_job.py` - `GenerationLoop.run(..., panel_id=...)`.
- Tests: `test_review_gates.py` (25), `test_edit_invalidation.py` (13), `test_anima_autopilot_e2e.py` (11).

Design decisions worth knowing:

- Four gates: `story`, `storyboard`, `artwork_early`, `artwork_final`. The plan named three; Artwork needs two checkpoints because the cadence is "render page one, ask, then render the rest, ask again before lettering".
- Waiting uses a per-gate `asyncio.Event` on the coordinator, not the run's pause event. A run waiting for a review is not a user who pressed pause, and merging them would make "resume" ambiguous.
- The spec-7.2 state machine was left alone. The gate a run is blocked on is exposed as `run.awaiting_review` and in `to_status()`, rather than adding states that would need new `_FORWARD` edges.
- `ReviewPolicy.for_profile` returns an empty gate list for anything that is not `anima_*`, and an empty policy reports every gate approved. Legacy projects therefore run exactly as before, with no branch in the orchestrator.
- Decisions are idempotent: repeating the standing decision records nothing. A changed decision appends, so the history shows approve-then-reject.
- Invalidation marks, never deletes. `apply_invalidation` sets panels back to `draft`, appends an `invalidated` history entry, and deliberately keeps the old `image_path` so the previous artwork stays visible until something replaces it. No GPU work is started.
- A dialogue edit does not touch panel images - only `bubbles`, `page_render`, `exports`. A continuity edit invalidates the edited panel and everything after it in reading order, because scene state flows forward.

Bug found and fixed while wiring the E2E:

- `GenerationLoop` derived its panel id as `panel_{panel_number:03d}`, which is unique only within a page, so page 2's first panel overwrote page 1's candidate image (`{candidate_id}.png`). `run()` now accepts an explicit `panel_id`, and `run_panels_sequentially` passes each record's own id. The old default is unchanged for existing callers. Covered by `test_panels_from_different_pages_do_not_overwrite_each_other`.

Verification (2026-08-27, Claude Code):

```powershell
$env:TEMP='<a temp dir the agent can write to>'
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m pytest tests/backend/test_review_gates.py tests/backend/test_edit_invalidation.py tests/backend/test_anima_autopilot_e2e.py tests/backend/test_autopilot.py tests/backend/test_autopilot_resume_e2e.py -q -p no:cacheprovider
```

Result: `72 passed`.

Full backend suite: `7 failed, 998 passed, 15 skipped` in 74s - the same 7 pre-existing portability failures, no regressions.

`.\.venv\Scripts\python.exe -m ruff check src/manga_autopilot tests/backend` reported `All checks passed!`.

## Task 8 result

Delivered files:

- `web/review_editor.js` - review status display, per-gate form fields, stale markers, approve/reject actions. No free-drag, undo/redo, or image diff.
- `web/index.js` - a Reviews tab that mounts it.
- `examples/workflows/anima_turbo.workflow.json` + `.registry.json` - a single-panel API-format graph with bindings.
- `docs/anima_mvp.md` - setup, licence, profiles, resolution policy, prompt order, review API, invalidation table, snapshots and privacy, migration, preflight codes, managed LM Studio, how to run the tests.
- `README.md` / `README.ja.md` - an Anima MVP section and a docs link in both.
- Portability fixes: `AssetRef.path` is normalised to POSIX at the model boundary; `test_release_readiness.py` reads with explicit UTF-8; `test_artifact_store.py` uses `tmp_path` instead of `/tmp`.
- Tests: `test_review_editor_ui.py` (14), 8 added to `test_example_workflows.py`, 2 added to `test_character_model.py`.

How the example workflow was derived:

- Source: the verified `anima_two_prompt_v1.json`, branch A only (nodes 84, 81, 80, 78, 76, 77, 79, 82, 83, 46).
- Every node's API input names were read from the live `/object_info` rather than guessed, including `LoraLoader|pysssss` (`model`, `clip`, `lora_name`, `strength_model`, `strength_clip`).
- The source's user-specific positive prompt and `anima_two_prompt/...` output prefix were replaced with neutral content; a test asserts none of it leaked.
- Two deliberate deviations from the source graph, both documented in `docs/anima_mvp.md`:
  - `ResolutionSelector` was dropped and `EmptyLatentImage` takes literal dimensions. A node that computes resolution independently would fight the profile, which owns resolution.
  - The source wires the negative `CLIPTextEncode` to the raw CLIP and the positive one to the LoRA-adjusted CLIP. That asymmetry is preserved as verified rather than "corrected".

Front-end testing approach:

- The pure helpers in `review_editor.js` are executed for real through Node when it is installed, and those tests skip when it is not, so a Node-less CI still runs everything else. Node v24.18.0 was present locally and the tests ran.
- The structural checks strip comments before scanning, so the module's own prose about what it does not implement cannot satisfy them.

Final verification (2026-08-27, Claude Code):

```powershell
$env:TEMP='<a temp dir the agent can write to>'
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest tests/backend -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest -m release_gate -q -p no:cacheprovider
```

- `ruff check .` -> `All checks passed!`
- `pytest tests/backend` -> `1029 passed, 15 skipped` in 76s. **No failures.**
- `pytest -m release_gate` -> `6 passed, 1038 deselected`
- `git diff --check` -> clean

The suite started this work at `799 passed, 11 failed`. All pre-existing Windows portability failures are fixed; none were worked around by weakening a production assertion.

## First live render (2026-08-27)

One panel was generated on the user's GPU through the project's own code path:
profile -> `AnimaPromptBuilder` -> preflight against the live `/object_info` ->
workflow bindings -> `ComfyExecutor` -> ComfyUI -> image -> run snapshot. Nothing
was written into the repository; the output went to a scratch directory.

- Preflight passed with no errors against the real install.
- 960x1280, 12 steps, CFG 1, `er_sde`/`simple`, seed 20260827.
- **12.2 seconds** on an RTX 5070 Ti, with roughly 3.5 GB VRAM free at submit time.
- The image matched every semantic segment: short black hair, green eyes, school
  uniform, umbrella, low angle, rainy street at dusk, wet reflections.

Two real defects were found by doing this, both now fixed and committed:

1. **The negative prompt never reached ComfyUI** (`1ba21d0`). `ComfyExecutor`
   submitted `PromptSpec.negative` rather than `negative_full()`, so the
   application's text/watermark bans were dropped, and run snapshots - which
   record `negative_full()` - disagreed with what was actually rendered. Two
   existing tests had pinned the buggy behaviour; both were corrected to assert
   the bans are present rather than that they are absent.

2. **At CFG 1 the negative prompt does nothing at all** (`ef2a5c3`). The rendered
   image showed Japanese signage text despite `text` and `watermark` being in the
   negative prompt. `comfy/samplers.py` in the installed ComfyUI 0.30.0 sets
   `uncond_ = None` when `cond_scale` is close to 1.0, so the negative branch is
   never evaluated. `anima_turbo` runs at CFG 1, so its `negative_defaults` and
   the application bans are inert; `anima_base` and `anima_aesthetic` are not
   affected. Preflight now emits `prompt.negative_inert_at_cfg1`, and
   `docs/anima_mvp.md` explains the workaround (express suppression positively).

Note that fix 1 is therefore inert for `anima_turbo` specifically, and matters
for the two higher-CFG profiles. Both fixes are still correct.

Suite after both fixes: `1032 passed, 15 skipped`, `ruff check .` clean,
`git diff --check` clean.

## Prompt safety follow-up (2026-08-27)

The CFG 1 finding raised the obvious next question: if the negative prompt is
inert for Turbo, how does a strict run suppress anything? The answer is not a
profile-level "positive suppression" list, and that matters enough to record.

A diffusion model renders the noun regardless of the grammar around it. Anima was
observed rendering a bikini in all 28 scenes of a run whose text read "the bikini
has been fully removed"; deleting the word outright was the only fix. So moving
bans into the positive prompt as "no text, no watermark" would make things worse,
not better - it is a second, separate failure from the CFG 1 one, and it happens
at any CFG.

What was implemented instead (`7556e87`): `AnimaPromptBuilder` lints the rendered
positive prompt and warns, naming each negation. `AnimaPromptBuilder(
reject_negations=True)` raises instead. `find_negations(text)` is public.

Checked against the recorded reproduction: the failing phrasing is flagged, its
documented fix passes clean, the shipped example workflow's prompt passes clean,
and ordinary vocabulary (`snow`, `notebook`, `nostalgic`) is not flagged.

What was deliberately NOT implemented: a `positive_suppression` field on the
profile. There is no scene-independent affirmative phrasing that reliably
suppresses text or watermarks, and inventing one without experimental evidence
would be guessing. Finding such phrasings is prompt-design work that needs real
generations, and it belongs with the quality iteration below.

Suite: `1038 passed, 15 skipped`, `ruff check .` clean, `git diff --check` clean.

## Route-level integration (2026-08-27)

The earlier E2E drove the `Orchestrator` directly, which left the HTTP path
untested: coordinator publication, the early-artwork cadence in the route hook,
and an approval travelling from a REST call back into a running pipeline. Two
tests now cover it with a fake LLM and a fake executor, no GPU:

- `test_the_route_path_pauses_at_every_gate_and_resumes_over_rest` walks a
  two-page project through all four gates over the real endpoints, asserting the
  executor is untouched before Storyboard approval and that only page 1 renders
  before the early artwork review.
- `test_a_rejection_over_rest_stops_the_route_run` proves a rejected story never
  reaches the executor.

Writing them exposed a real defect, now fixed (`377e321`):

**Characters were never created by the autopilot.** `define_characters` passed a
`CharacterSpec` (the planner's free-text output) straight to
`CharacterService.create()`, which takes a `Character`. The AttributeError was
caught by a bare `except` and logged as a warning, so a strict run carried on
with an empty character list and failed several steps later with the misleading
`character 'char_hero' is not defined` from panel planning.

`spec_to_character()` now does the conversion: it reads hair and eye colour out
of the trait list, keeps every trait verbatim under `distinctive_features`, and
writes `unspecified` where the planner said nothing rather than inventing a
value. A strict run re-raises instead of swallowing, so the failure surfaces
where its cause is visible.

The same run also confirmed Task 1's strict validation works from the route: the
first attempt failed with `Story Bible is required in strict mode` because the
fake planner had not supplied one.

Suite: `1045 passed, 15 skipped`, `ruff check .` clean, `git diff --check` clean.

## Full live run, and what it cost to get there (2026-08-27/28)

A two-page project now runs end to end on real hardware: real planner, real GPU,
real HTTP routes, all four review gates approved over REST.

```
plan_story    -> 「最後の配達」2 pages
plan_panels   -> 9 panels (a repair fixed panel order [1,4,3,2,5])
planner unloaded before generation, VRAM handed back
page 1        -> 5 panels rendered, early artwork gate blocked
page 2        -> 4 panels rendered
COMPLETED, with page PNGs, webtoon slices and a PDF
```

Two full runs completed: 197.2s and 1597.5s. The difference is the planner, not
the pipeline - see the timing note below.

### Every defect this exposed

Nine live defects, each committed with a test. Seven were things that were built,
tested, and never called from `src/`:

| Commit | Defect |
|---|---|
| `1ba21d0` | `negative_full()` never reached ComfyUI, so the text/watermark bans were dropped and snapshots disagreed with what rendered |
| `ef2a5c3` | At CFG 1 ComfyUI skips the negative branch entirely; `anima_turbo`'s negatives are inert |
| `377e321` | `define_characters` passed a `CharacterSpec` where a `Character` was required; the bare `except` hid it and a strict run silently had no characters |
| `b3ce2d8` | `AnimaPromptBuilder` was never called from `src/`; the strict route still used the LLM-driven builder, so the profile did not own the technical fields |
| `8503fbb` | Four at once: `lms` output decoded with the Windows ANSI code page; LM Studio addresses models by identifier so requesting the model key JIT-loads a second copy; the orchestrator waited at a gate after the step had failed; an empty reasoning response was reported as "could not extract JSON from ''" |
| `86699ab` | `llm.endpoint` ending in `/v1` produced `/v1/v1/...`, which LM Studio answers 200 with an error body, surfacing as an empty completion |
| `a8c2ac8` | `PAGES_PLANNED` and `PANELS_PLANNED` were missing from the failure map, so a planning failure left the run walking on to ask for review of artwork that was never made. The planner had also never been told which layouts exist, so it invented ids that validation then rejected |
| `43e2572` | A whole sentence was read as a character's hair or eye colour |
| `be7af23` | An unreachable ComfyUI reported only `FAILED_PANEL_GENERATION` |
| `ed58f61` | `RunSnapshotWriter` and `FingerprintCache` were never called from `src/`; a completed run left no `snapshot.json` |
| `c326104` | Neither provider set a timeout, so aiohttp's 300s default applied; a bare `TimeoutError` stringifies to "" |

The pattern worth remembering: **a component with tests is not a component in
use.** `grep -rn "<name>" src/` after adding a public entry point is cheap.

### Both new warnings fired on real data

- `prompt.negative_inert_at_cfg1` on every strict Turbo run.
- The negation lint caught the planner writing `No words` into a panel
  description - exactly the phrasing that renders the thing it forbids.

### Timing measured on this machine (RTX 5070 Ti)

| Step | Time |
|---|---|
| Panel render | ~12s each, 9 panels in ~35s |
| Planner load / unload | 6-11s |
| Story plan (gemma-4-12b) | 66.8s, 230.9s, and 651.2s across runs |
| Story plan (qwen3.5-9b) | 296.7s, with 14,021 characters of reasoning |

Planner latency is the dominant cost and varies by an order of magnitude between
identical runs. `llm.timeout_sec` defaults to 900 for this reason; the 651.2s run
would have failed under aiohttp's 300s default.

### The snapshot, verified on disk

`runs/{run_id}/snapshot.json`, 10,388 bytes: the four model SHA-256 fingerprints,
all nine rendered prompts with seeds and dimensions, the profile and workflow
hashes, the environment, the LLM settings - and no credentials. Fingerprints
need `comfyui.install_root`; resolution reads `extra_model_paths.yaml`, because
a Desktop install keeps nothing under `models/`.

## Speech bubbles, and one violated constraint (2026-08-28)

The completed run's pages showed correctly drawn bubbles containing nothing.
Two separate causes, both now fixed.

### The text was never drawn (`9533265`)

`ImageDraw.text` was called with no `font=`, so Pillow used its built-in bitmap
face: no CJK glyphs, and the requested size ignored. The `FontSpec` (family,
size, colour) was modelled, persisted in `bubbles.json`, and never handed to the
renderer - the fourth built-tested-unwired component found this week.

`services/fonts.py` resolves a family to a real file: a caller-supplied
directory first, then the platform font directories, then any CJK-capable face
so Japanese still renders. Two layout defects surfaced once glyphs appeared:
vertical text sat on the bounding box edge and spilled outside the ellipse, and
text longer than the box was cut off (a bubble sized for two characters rendered
the three-character line as two). Text is now centred inside the ellipse and
shrinks to fit, with a readable floor before truncation.

### Six of nine bubbles were invented (`f3a4798`)

The lettering hook gave every panel without planned dialogue a hardcoded line,
commented "so the page is never bare". The live planner had written dialogue for
three of nine panels; the other six carried that line, unrelated to the art.

HANDOFF already stated the constraint - "No invented dialogue fallback in strict
Anima lettering" - so this was a violated decision, not a quality judgement. A
strict run now leaves a silent panel silent; generic projects keep the
placeholder, promoted to a named constant.

Replaying lettering on the finished run: 9 bubbles became 3, and the surviving
lines are the planner's own.

Suite: `1079 passed, 15 skipped`, `ruff check .` clean, `git diff --check` clean.

## Character drift: the identity never reached the prompt (2026-08-28)

Hair and eye colour changed between panels of the same page. The run snapshot
settled it in one read: every rendered prompt ran

```
masterpiece, best quality, score_7, safe, <purpose>, <background>, <camera>, <emotion>, anime, manga
```

with **no identity and no must_keep terms at all**. Not weak adherence - nothing
to adhere to.

The character records were detailed and correct ("Messy black hair with a
slightly damp texture from the rain, Sharp, tired grey eyes...", one per
character). Every panel's `characters` list was empty, so
`segments_from_panel_plan` had nothing to look up.

Why it was empty: the panel prompt's rules listed the fields to fill -
`panelNumber, purpose, shot, action, emotion`, plus optional dialogue and sfx -
and **never mentioned `characters`**. Semantic validation only rejects *unknown*
ids, so an empty list passes trivially; the repair loop can even satisfy an
unknown-character error by deleting the ids.

This is the same shape as the layout defect from the day before: a field
required downstream that nothing upstream asked for.

Fixed in `f6aaab9`:

- The panel prompt now asks for the ids, drawn from the Active Characters roster
  that was already being supplied as context.
- A strict panel that names nobody while characters exist logs a warning saying
  its appearance is unanchored, instead of rendering a stranger in silence.

The existing unknown-id guard is unchanged and covered by a test, so the fix
cannot be satisfied by inventing ids either.

**Verified on hardware (2026-08-28).** A live two-page run with the fix:

- Panels naming a character: **6 of 9**, up from 0 of 9. The three that name
  nobody are establishing shots and a prop close-up, which is correct.
- Every one of those six rendered prompts now opens with the appearance:
  `Messy, dark navy hair soaked by rain..., Tired grey eyes...` before the
  scene description, exactly as the segment order intends.
- The new warning fired for the three unanchored panels, naming them.
- Visually the courier is the same person across panels: same dark navy hair,
  grey eyes, black trench coat and brown leather satchel, and the satchel
  matches its own close-up panel. The recipient is likewise stable across the
  three panels she appears in.

Run: 1720.3s end to end, `COMPLETED`, nine panels, pages/webtoon/PDF exported.
The user's own LM Studio model was resident throughout and was left loaded:
`LM Studio after: ['google/gemma-4-12b-qat']`.

## What is not done

Nothing here is a known defect. Every problem found by running the thing has been
fixed and confirmed on hardware. What is left is a decision, a gap in coverage,
and tuning.

### Needs a decision

1. **CI has never run on this branch, and neither approval is ours to give.**
   The upstream run for PR #219 is `action_required` - GitHub holds workflow
   runs from a first-time fork contributor until a maintainer approves, and we
   have only `READ`. The fork has no workflows registered either: GitHub does
   not enable them on a fork until someone opens its Actions tab and confirms.
   Until one of those happens, the only evidence is the local suite.
2. **The tag.** `v0.1.0-rc1` is already tagged and sits on `main`; this branch
   declares `0.1.0-rc2`. Tagging happens on `main` after the PR merges, which
   is the upstream's call.
3. ~~Preflight that cannot run.~~ **Settled 2026-08-28.** The question assumed
   a binary that does not exist: only two of the eight checks need
   `/object_info`. Preflight now runs the other six whatever the wiring, and a
   missing client is not itself a failure - `manga_remote_executor` is a
   supported deployment with no local ComfyUI to interrogate. See "Preflight,
   and the settings that never reached it".

### Gap in coverage

3. ~~The extension has never been loaded by ComfyUI itself.~~ **Closed
   2026-08-28.** Installed into the running ComfyUI, restarted, and driven in
   the browser down to a real HTTP round trip. See "Loaded by ComfyUI, end to
   end". It found two defects, both fixed. What is still untested there is the
   rest of the workspace - Page Editor, Character Manager, Progress and Export
   Center were never opened against a real project, because that needs a
   project to exist.
4. **The documented install path is still unverified.** `README.md` and
   `docs/install.md` tell the user to run `pip install -e .`; the live test used
   a junction and no editable install, because the running venv already had
   every dependency. Nothing has confirmed that the documented route works.
5. **The extension is still installed** in the running ComfyUI as a junction.
   Whether it stays is the user's call; "Loaded by ComfyUI, end to end" has the
   removal command.

### Tuning, not repair

6. **Planner latency dominates.** The same two-page plan measured 66.8s, 230.9s,
   420.4s, 651.2s and over 900s; panel rendering is ~12s each by comparison.
   Most of the time is chain of thought that is discarded. Each run now records
   `planner.reasoning_ratio` in its snapshot, so the next run's own numbers can
   settle whether a non-reasoning planner is worth switching to. `docs/anima_mvp.md`
   has the measurements and the guidance.
7. **The planner writes little dialogue** - three of nine panels in the last run.
   Prompt design, not code; inventing the rest is explicitly forbidden.
8. **Establishing shots carry no character anchor**, by design, and currently
   warn. Whether they should is worth deciding once more pages exist.
9. **Turbo suppression must be phrased positively.** At CFG 1 the negative prompt
   is not evaluated at all, and negation in the positive prompt renders the very
   thing it names. No scene-independent phrasing has been found; that needs
   experiments against real output.

## Preflight, and the settings that never reached it (2026-08-28)

The open question was "should a strict run hard-fail when preflight cannot
run?" Reading the code first dissolved most of it, and turned up a defect the
question was hiding.

**Only two of the eight checks need a live server.** Classified from the AST of
`AnimaPreflight`:

| Check | Needs `/object_info` |
|---|---|
| `_check_endpoint`, `_check_license`, `_check_negative_prompt_reachability` | no |
| `_check_resolution`, `_check_references`, `_check_output_dir` | no |
| `_check_models`, `_check_workflow` | yes |

The gate used to return early when `manga_comfy_client` was absent, which took
the licence acknowledgement and the remote-endpoint auth check down with the two
that genuinely could not run. `AnimaPreflight.capabilities` is now
`ComfyCapabilities | None`; with `None` it runs the six and records
`comfy.capabilities_unavailable` as a warning.

Hard-failing on a missing client would have been wrong. `panel_routes._executor`
resolves four executors in order, and only the fourth uses
`manga_comfy_client` + `manga_workflow_registry`; `manga_remote_executor` (the
Modal GPU bridge) is a documented deployment where there is no local ComfyUI.

**The defect.** `docs/anima_mvp.md` tells the user to `PATCH /projects/{id}`
with `generation_profile_id` and `license_acknowledged`, and states "Preflight
refuses to generate until this is set". Both land on the project. But
`_is_anima_run`, strict mode and the preflight all read `run.input`, and
`run.input = dict(input_payload or {})` was built from the start request body
alone. The review coordinator *does* read the project (`autopilot_routes.py`
`_open_review_coordinator`), so a project configured exactly as documented and
started with no body got the Anima review gates while strict mode stayed off and
preflight never ran. The documented promise did not hold. Another instance of
the pattern in "tested but unwired".

`_seed_input_from_project` now fills the run input from the project's persisted
settings before the run starts; an explicit start body still wins.

The proof is that `test_anima_autopilot_e2e.py` needed no change. It already
acknowledged the licence the documented way - `PATCH` on the project - and
passed the profile in the start body. Once preflight stopped stepping aside, the
run failed with `license.not_acknowledged`; once the seeding landed, it passed.
Both fixes were confirmed to be load-bearing by reverting them: removing the
seeding call fails that E2E test, and disabling the capability gate fails eight
preflight tests.

`test_strict_mode_comes_from_the_project_not_the_start_body` pins it: the
profile and licence are set only by `PATCH`, and the run still enters strict
mode, pauses at every gate and passes preflight. Removing the seeding call
fails it.

**The scope stops deliberately, and there is a live consequence.** The restart
path also restores `page_count`, `candidate_count`, `max_retries`, `threshold`
and `title` from `project.json`; start still restores none of them. Seeding
them is not a cleanup - the project defaults (`candidate_count` 4,
`max_retry_per_panel` 5, `quality_threshold` 0.78) differ from the ones start
falls back to (1, 1, 0.5), so it would quadruple the candidates generated for
every caller that omits them. That is a GPU cost decision.

The consequence is that `page_count` still reaches a run **only** through the
start body. A project started exactly as `docs/anima_mvp.md` describes - no
body - plans against `page_count` 1, so any story plan with more than one page
dies at `FAILED_STORY_PLANNING` with "expected 1 pages, received 2". This was
observed, not reasoned: an earlier version of the test above did start with no
body and failed that way. So the documented sequence cannot currently produce a
multi-page run. Fixing it means deciding what start should take from the
project, which is the same cost decision.

## Loaded by ComfyUI, end to end (2026-08-28)

The coverage gap is closed. The extension was installed into the ComfyUI that
actually runs on this machine, ComfyUI was restarted, and the whole chain was
exercised in the browser.

**Two ComfyUI installations exist here; only one runs.** Getting this wrong
wasted a cycle, so it is written down:

| | Running | Not running |
|---|---|---|
| Managed by | Stability Matrix | Comfy Desktop |
| ComfyUI | `%LOCALAPPDATA%\Comfy-Desktop\Data\Packages\ComfyUI` (0.30.0) | `%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Installs\Koudai\ComfyUI` (0.30.2) |
| Interpreter | `<pkg>\venv\Scripts\python.exe` (3.12.10) | `<install>\standalone-env\python.exe` (3.13.12) |
| Frontend | `comfyui_frontend_package 1.47.11` | `1.45.21` |
| `custom_nodes` | ~30 packs (`comfyui-custom-scripts`, `ComfyUI-Anima-Resolution`, ...) | stock only |

The directory named `Comfy-Desktop\Data` is a *Stability Matrix* data root -
it holds `StabilityMatrix.db`, `Packages`, `Assets`. `installations.json`
under `%APPDATA%\Comfy Desktop` describes the **other**, idle installation.
Identify the live one from the running process, not from config files:

```powershell
(Get-CimInstance Win32_Process -Filter "ProcessId=$((Get-NetTCPConnection -LocalPort 8188 -State Listen).OwningProcess)").CommandLine
```

Installed as a directory junction, so edits in this repository are live in
ComfyUI without copying:

```
mklink /J "<pkg>\ComfyUI\custom_nodes\ComfyUI-Manga-Autopilot" "C:\Claude Code\comfyui-manga-autopilot"
```

No dependency had to be added: `jsonschema`, `aiohttp`, `pydantic`, `yaml` and
`PIL` all resolve in that venv (`jsonschema` arrives via one of the other node
packs). Restarted with `POST /api/v2/manager/reboot` after confirming the queue
was `0/0`; it came back in well under a minute.

What the running instance shows:

- `GET /manga_autopilot/api/health` -> `{"ok": true, "service":
  "manga_autopilot", "version": "0.1.0-rc1"}`
- `GET /api/extensions` lists all six files under
  `/extensions/ComfyUI-Manga-Autopilot/` - ComfyUI globs the web directory
  recursively, so every `.js` is imported, not just `index.js`. Harmless here:
  only `index.js` has a module-scope side effect, and the five mount modules
  resolve to the URLs `index.js` already imports.
- `app.extensionManager.sidebarTab.sidebarTabs` contains `manga-autopilot`
  beside the five core tabs; its `type` is `custom` and its title is
  `Manga Autopilot`.
- Opening it mounts `.manga-autopilot-root` with all six views.
- Setting a project id and opening Reviews issues
  `GET /manga_autopilot/api/projects/smoke-check-nonexistent/reviews` -> 404
  and renders `Could not load reviews: unknown project: ...`, which is the
  backend's own message. Frontend, routes and backend are wired together
  inside ComfyUI.

Two defects only this could find, both fixed in `3ad0d94`: the Projects tab was
unreachable behind its own guard, and the workspace was ~448px wide inside a
312px panel that clips on the x axis, leaving the left edge cut off. After the
fix the root sits at x=59 with `scrollWidth` 312 and every control lies inside
the panel.

Two notes for whoever drives this pane next:

- Synthetic clicks do not land. A PrimeVue overlay (`p-blockui-mask`) is stuck
  with both `-enter` and `-leave` classes because a pane that is not displayed
  never composites, so the transition never ends. Drive the DOM directly
  (`element.click()`), or display the pane. This is an artifact of the
  automation surface, not of ComfyUI.
- Do not send keystrokes to the canvas. With no field focused they are
  ComfyUI shortcuts.

The extension is still installed. Remove it with
`rmdir "<pkg>\ComfyUI\custom_nodes\ComfyUI-Manga-Autopilot"` (a junction -
`rmdir` removes the link, never the target) and restart.

## requirements.txt, and what ComfyUI actually installs (2026-08-28)

ComfyUI and ComfyUI-Manager install a node pack with its `requirements.txt`;
neither reads `pyproject.toml`. This repository had no `requirements.txt`, so a
ComfyUI without `jsonschema` would import the package fine and then fail inside
`register_all` - `routes/__init__.py:158` pulls in `autopilot_routes`, which
imports `Draft202012Validator` at module scope.
`attach_routes_to_prompt_server` catches that, logs `Failed to register Manga
Autopilot routes` and returns `False`, so it is not silent - but the sidebar
tab still renders, and the extension would look installed with no HTTP API.

ComfyUI's own `requirements.txt` declares `aiohttp`, `pyyaml`, `Pillow` and
`pydantic`, and not `jsonschema`. The venv here happens to have it, so this
machine would not have hit the failure; a clean ComfyUI would.

Added in `e6b408b` with `tests/backend/test_comfyui_requirements.py`, which
holds the file to `[project].dependencies` and fails if a module-scope
third-party import in `src/` has no requirement behind it. Both tests were
confirmed to fail with `jsonschema` removed from the file. `docs/install.md`
and `docs/install.ja.md` now say to install with ComfyUI's own interpreter and
name the symptom.

One trap, from reading `nodes.py`: do **not** add `web = "web"` under a
`[tool.comfy]` section in `pyproject.toml`. `nodes.py:2264` would then register
the same directory under the *project* name (`comfyui-manga-autopilot`)
alongside the *folder* name (`ComfyUI-Manga-Autopilot`), the frontend would
import `index.js` twice under two URLs, and `app.registerExtension` would run
twice for one extension.

## Review editor, verified in a browser (2026-08-28)

`mountReviewEditor` was driven against a stubbed backend in a real browser. It
mounts, lists the gates with their statuses, marks the blocking one, shows that
gate's own form fields, posts approve with the typed note, advances as the board
changes, and ends on "Every review is approved." with the form and buttons gone.
The request log read `GET -> POST {note} -> GET`.

That run found a defect the structural tests could not reach: a decided gate put
its note in `title`, which becomes the accessible name, so a screen reader heard
"premise reads fine" instead of "Story: Approved". The visible text was correct
throughout, which is exactly why nothing else caught it. Fixed in `284868d` with
an `aria-label` carrying both, and pinned by a test.

The harness was a scratchpad page importing the module over a local static
server, with `fetchImpl` stubbed - no ComfyUI, no backend, no GPU. Rebuilding it
takes a few minutes and touches nothing in the repository.

## `config.yaml` had no effect in a live ComfyUI install (2026-08-30)

Preparing to resume the live test from the session boundary above, before
touching the GPU: `_llm_provider` (`autopilot_routes.py`) reads
`app["manga_llm_provider"]`, falling back to `app["manga_llm_settings"]`, and
only then to `ManualProvider` - a no-op that returns `"{}"` for every
planning call. `attach_routes_to_prompt_server` (`comfy_integration.py`),
the only code path that runs inside a real ComfyUI, never set either key.
`config.py`'s `load_config`/`discover_config_path` had no caller anywhere in
`src/` outside their own module - another instance of "a component with
tests is not a component in use" (see "Every defect this exposed" above).

Contrast with `workflow_routes._comfy_client`: that one lazily builds a real
`ComfyClient` pointed at `127.0.0.1:8188` the first time it's needed. Nothing
equivalent existed for the LLM side, and no HTTP route could set it either -
grepping every test file was the only way `app["manga_llm_provider"]` was
ever assigned outside test fixtures.

This means the two-page live run recorded above ("Full live run, and what it
cost to get there") did not get its real LLM provider from anything in this
repository. Whatever set it up lived in the session's scratchpad harness,
never committed.

**Fixed**: `comfy_integration.py` now builds a real provider from
`config.yaml` (discovered next to the extension's own root, or
`$MANGA_AUTOPILOT_CONFIG_PATH`) once at startup, bridging `config.py`'s
`LLMSettings` (`provider`/`endpoint`/`model`, the `config.yaml` schema) to
`services.llm_provider.LLMSettings` (`type`/`endpoint`/`model`, what
`build_provider` actually consumes) - two separate models that nothing
connected before. `lm_studio` is accepted as a friendlier alias for the
generic `openai_compatible` type. With no `config.yaml` at all, the result is
still `config.py`'s own documented default (Ollama at `127.0.0.1:11434`)
rather than the silent no-op - a real attempt at what was already promised,
not a new default. An existing `app["manga_llm_provider"]` (tests, or a
future caller) is left untouched; a failure to build one is logged and never
fatal, matching `attach_routes_to_prompt_server`'s existing contract.

Six tests pin this in `test_comfyui_integration.py`: the Ollama default with
no config, an `openai_compatible` config, the `lm_studio` alias, the
`$MANGA_AUTOPILOT_CONFIG_PATH` override, that `attach_routes_to_prompt_server`
now always leaves a real provider behind, and that it never clobbers one
that was already set.

A local, untracked `config.yaml` (now covered by `.gitignore`) points this
machine's live ComfyUI at whatever LM Studio has loaded, for today's live
test.

**A second instance, found by actually running it.** After that fix and a
ComfyUI restart, a real two-page run planned successfully end to end
(story, four characters, both pages, all seven panels) in under three
minutes using the real LM Studio model - faster than any prior planner
measurement, because `gemma4-12b-qat-uncensored-hauhaucs-balanced` produced
no discarded reasoning. It then failed instantly at `generate_panels` with
`HTTPServiceUnavailable`. Cause: `panel_routes._executor` requires
`app["manga_comfy_client"]` to already exist and has no lazy default,
unlike `workflow_routes._comfy_client`, and registering a workflow (the only
setup step this session's harness had run) never touches that key - only
`workflow_routes.test_run_workflow` does. Fixed the same way: build a real
`ComfyClient` from `config.yaml`'s `comfyui:` section (or its documented
`127.0.0.1:8188` default) once at startup, mirroring the LLM fix. Four more
tests in `test_comfyui_integration.py`.

## Third live run: completed end to end (2026-08-30)

With both wiring fixes above in place and ComfyUI restarted, a two-page
`anima_turbo` run completed in full: `story -> characters -> pages -> 7
panels -> COMPLETED` in 3m28s (01:43:29 - 01:46:57 UTC), producing two page
PNGs, a webtoon strip, and `manga.pdf` under the project's `exports/`, plus a
`snapshot.json` recording all seven prompts, seeds, and the real planner
cost (12 calls, 382s, 75.3% of generated text was discarded reasoning -
`gemma4-12b-qat-uncensored-hauhaucs-balanced` still reasons some, just far
less than the models measured earlier). No credentials in the snapshot,
matching the documented guarantee.

Two harness mistakes on the way there, neither a code defect:

- The monitoring loop read `run.get("status")`; the API returns `state`.
  With the wrong key it never noticed a run had already finished (twice),
  and sat polling until its own timeout. Fixed in the scratchpad script,
  not the repository - this was a bug in the throwaway harness, not
  `routes.autopilot_routes`.
- The start body needs an explicit `"workflow_id": "anima_turbo"`; omitting
  it falls through to the generic `"anime_t2i_default"` placeholder
  (`autopilot_routes._default_workflow_id`), which no registered workflow
  answers to. `docs/anima_mvp.md` names the registry payload but never
  says the start body must repeat its `workflow_id` - worth a line there
  for whoever runs this next, though it did not block today's run once
  understood.

One earlier attempt (this session, before the workflow_id fix) also failed
at `plan_pages` with `ValueError: layout 'layout_1' is not registered` -
the planner inventing a layout id outside the registered catalog. Strict
validation rejected it correctly (matches "Strict planning rejects an
unregistered layout id" in `docs/anima_mvp.md`); the retry planned valid
ids without any code change, so this reads as planner-output variance with
this specific model, not a reproducible defect. Worth watching if it
recurs.

One a loose end from `completion_report`: `panels_total: 0, panels_passed:
0` despite seven real rendered panels and two exported pages - a reporting
field that never got the count, not a run that generated nothing.
Unverified whether this predates today; not investigated further, kept
here so it isn't mistaken for a fresh regression.

The extension is still installed as a junction and `config.yaml` (gitignored,
local to this machine) still points at whatever LM Studio has loaded.

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
- ComfyUI version: `0.30.0` - this is the Stability Matrix package at
  `%LOCALAPPDATA%\Comfy-Desktop\Data\Packages\ComfyUI`, not the idle Comfy
  Desktop install; see "Loaded by ComfyUI, end to end"
- Python: `3.12.10` (`<pkg>\venv\Scripts\python.exe`)
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

All eight tasks are implemented, verified, committed, and proven on hardware.
Everything that remains is listed under "What is not done"; none of it is a
known defect.

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

The repository moved on 2026-08-27; this used to point at the deleted Codex
sandbox.

```powershell
Set-Location 'C:\Claude Code\comfyui-manga-autopilot'
git status --short
git log --oneline -4
Get-Content -LiteralPath HANDOFF.md -Encoding utf8
```
