# Claude Code handoff: Anima Manga Autopilot MVP

Updated: 2026-08-27 JST (Claude Code, Tasks 1-8 complete + first live render)

## Objective

Continue implementing the Anima MVP described in:

`C:\Users\kouda\Downloads\codex_manga_autopilot_anima_requirements_v1.0.md`

The user accepted the recommended choices from the prior grilling session and asked that the remaining choices also use the recommendations. The agreed implementation plan is:

`docs/superpowers/plans/2026-08-26-anima-mvp.md`

Every task in that plan is done. The remaining work is live acceptance, which needs explicit approval; see "What is not done".

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

## What is not done

The plan is complete, the suite is green, and one panel has been rendered for real. Still outstanding, each needing explicit user approval:

1. **Live LM Studio acceptance** with a real planner model. `qwen3.5-9b` (6.55 GB) is **already installed locally** - checked with `lms ls` on 2026-08-27 - so this needs a load, not a download. Loading it is GPU/VRAM work and still needs the user to approve it. The strict path has never been driven by a real planner; the live render above used hand-written semantic segments.
2. **A full page and a full run.** One panel is proven end to end; a multi-page run through the review gates with real artwork is not.
3. **Quality iteration** on real output - prompt wording, profile choice, layout catalogue - which is unpredictable in duration. The CFG 1 finding means Turbo prompts have to carry suppression positively, which is a prompt-design task, not a code task.

Also left open deliberately: the route's preflight gate steps aside with a warning when the application has no `manga_comfy_client` or `manga_workflow_registry`. Whether a strict run should hard-fail when it cannot preflight is a product decision, not a bug to fix silently.

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

All eight tasks are implemented, verified, and committed. The next steps are the
live ones listed under "What is not done", and each needs the user to approve it
first.

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
