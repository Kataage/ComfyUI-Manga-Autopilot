# Anima MVP

Strict, reproducible manga generation with the Anima model family.

A project becomes "strict" when its `generation_profile_id` starts with `anima_`.
Every other project keeps the previous behaviour unchanged: generic prompting,
the old retry/fallback loop, and no review gates.

## What "strict" changes

| | Generic project | Strict Anima project |
|---|---|---|
| Prompt technical fields | LLM may propose them | The profile owns them |
| Prompt rendering | One LLM call per panel | Deterministic, no LLM call |
| Review | None | Four gates |
| Quality rejection | Auto-retry, then a fallback image | Stops and waits for you |
| Technical failure | Retried up to `max_retries` | Retried once |
| Panel persistence | Written once at the end | Written before the next panel starts |
| Missing prompt | Substituted with a placeholder | Run fails and says so |

## Setup

### 1. Install the models yourself

Nothing is bundled and nothing is downloaded automatically. Install these into
your ComfyUI:

| File | ComfyUI folder |
|---|---|
| `silvermoonmixAnima_v23.safetensors` | `models/unet` |
| `qwen_3_06b_base.safetensors` | `models/text_encoders` |
| `qwen_image_vae.safetensors` | `models/vae` |
| `anima-turbo-lora-v0.2.safetensors` (Turbo profile only) | `models/loras` |

The example workflow uses the `LoraLoader|pysssss` node from the
pythongosssss ComfyUI-Custom-Scripts pack. Swap it for the built-in `LoraLoader`
if you would rather not install that pack; the bindings do not touch it.

### 2. Accept the licence

Anima is published under the CircleStone Labs Non-Commercial License, and the
model card notes additional NVIDIA derivative-model terms. Read
<https://huggingface.co/circlestone-labs/Anima> and then set:

```
PATCH /manga_autopilot/api/projects/{project_id}
{"license_acknowledged": true}
```

Preflight refuses to generate until this is set. This project never accepts the
licence on your behalf.

### 3. Choose a profile

```
PATCH /manga_autopilot/api/projects/{project_id}
{"generation_profile_id": "anima_turbo"}
```

| Profile | Steps | CFG | Notes |
|---|---|---|---|
| `anima_base` | 30 | 4.5 | Model-card guidance for the base model |
| `anima_aesthetic` | 32 | 4.0 | Strips `score_*` tags from both prompts |
| `anima_turbo` | 12 | 1.0 | Mirrors the verified local Turbo workflow |

All three use `er_sde` / `simple`, one candidate per panel, one automatic
technical retry, and no automatic quality retry.

### 4. Register the workflow

`examples/workflows/anima_turbo.registry.json` is a ready-to-register payload
containing a single-panel API-format graph and its bindings.

## Resolution

Render dimensions are a pure function of the panel's aspect ratio. The panel's
absolute size is ignored.

1. Keep a pixel budget of 1,228,800 (960 x 1280).
2. Round each side to the nearest multiple of 64.
3. Clamp both sides to 512-1536.

| Panel aspect | Rendered |
|---|---|
| 3:4 | 960 x 1280 |
| 4:3 | 1280 x 960 |
| 1:1 | 1088 x 1088 |
| 8:1 | 1536 x 512 (clamped; preflight warns) |

An aspect that cannot be honoured inside the side limits is rendered clamped and
reported as a `resolution.aspect_clamped` warning, not silently.

## Prompts

The planner supplies semantic segments only. The builder orders them:

```
quality prefix, identity, must_keep, subject, action,
camera, emotion, background, lighting, style
```

Terms are comma-split, trimmed, and deduplicated case-insensitively keeping the
first occurrence, so identity survives truncation by the text encoder. The
application's text/watermark bans are always appended to the negative prompt -
though see the CFG 1 caveat below for when that has no effect.

`steps`, `cfg`, `sampler`, `scheduler`, and the dimensions come from the profile;
the seed comes from the run. If the planner emits `technical_overrides`, they are
accepted and then ignored, with a warning naming the ignored keys.

### The negative prompt is inert at CFG 1

`anima_turbo` renders at CFG 1. ComfyUI's `sampling_function` sets
`uncond_ = None` when `cond_scale` is close to 1.0, so **the negative branch is
not evaluated at all**. The effect is zero, not small, and the graph still looks
correctly wired, which is what makes it easy to miss.

Practical consequence: with the Turbo profile, `negative_defaults` and the
application's text/watermark bans do nothing. Express what you need to suppress
positively instead - "plain background" rather than a negative "signage, text".
`anima_base` (CFG 4.5) and `anima_aesthetic` (CFG 4.0) evaluate the negative
normally.

Preflight reports this as the `prompt.negative_inert_at_cfg1` warning.

Verified against `comfy/samplers.py` in ComfyUI 0.30.0 on 2026-08-27, and
observed in a live one-panel render: background signage text appeared despite
`text` and `watermark` being in the negative prompt.

### Never negate: delete the noun instead

A diffusion model renders the noun regardless of the grammar around it, so
negation in the **positive** prompt backfires too. This is a separate failure
from the CFG 1 one above and happens at any CFG.

Observed on Anima: a scene reading "the bikini has been fully removed" rendered
a bikini in all 28 scenes. Deleting the word entirely was the only fix.

| Don't write | Write instead |
|---|---|
| `no longer wearing the dress` | `her bare shoulders are visible` |
| `the hat has been removed` | `her hair is uncovered` |
| `without glasses` | `her bare eyes are visible` |
| `no signage` | `a plain concrete wall` |

`AnimaPromptBuilder` lints the rendered positive prompt and logs a warning
naming each negation it finds. `AnimaPromptBuilder(reject_negations=True)`
raises instead, for callers that would rather fail than render the wrong thing.
`find_negations(text)` exposes the same check.

Word boundaries keep ordinary vocabulary out of the results, so `snow`,
`notebook`, and `nostalgic` are not findings.

## Review gates

| Gate | When | What it protects |
|---|---|---|
| `story` | After story planning | The premise, before any character work |
| `storyboard` | Before any image is queued | Nothing reaches the GPU before this |
| `artwork_early` | After the first page renders | The art direction, before the rest of the pages cost GPU time |
| `artwork_final` | Before lettering | The finished artwork |

```
GET  /manga_autopilot/api/projects/{project_id}/reviews
POST /manga_autopilot/api/projects/{project_id}/reviews/{gate}/approve
POST /manga_autopilot/api/projects/{project_id}/reviews/{gate}/reject
```

Both decision endpoints accept an optional `{"note": "...", "by": "..."}` body.
Decisions are idempotent: repeating the standing decision records nothing. A
changed decision appends, so the history shows the whole conversation.

Decisions live in `reviews.json` inside the project, so an approval survives a
restart. Waiting uses a per-gate event, separate from the user's pause/resume, so
"resume" keeps one meaning.

The **Reviews** tab in the sidebar shows which gate is blocking, offers the
relevant fields as a form, and posts the decision.

## Editing and invalidation

An edit marks downstream work stale. It never deletes an image, never drops
history, and never starts GPU work - what to regenerate stays your call.

| Edit | Panel images invalidated | Stages marked stale |
|---|---|---|
| `dialogue` | none | bubbles, page render, exports |
| `image_only` | the edited panel (or the whole page) | panel images, page render, exports |
| `layout` | every panel on the page | panel images, bubbles, page render, exports |
| `continuity` | the edited panel and everything after it | panel images, bubbles, page render, exports |
| `character` | every panel that character appears in | panel images, page render, exports |

Invalidated panels return to `draft`, gain an `invalidated` history entry, and
keep their existing `image_path` so the previous artwork stays visible until
something replaces it.

## Reproducibility and privacy

Each run writes `runs/{run_id}/snapshot.json` containing the complete rendered
prompts, seeds, effective dimensions, profile and workflow hashes, model
SHA-256 fingerprints, LLM settings, and the runtime environment.

- Diagnostic logs carry the prompt **hash**, never the prompt text, so a debug
  log can be shared without shipping the prompts.
- Credentials never reach disk. Settings are scrubbed on the way in, and the
  snapshot writer refuses to serialise a document that still carries a
  credential-like key. Detection matches whole key names and singular suffixes,
  so `max_tokens` is kept and `access_token` is dropped.
- Model fingerprints record the file's name, size, and digest - never its
  absolute path.

### Exports

| Export | Contents |
|---|---|
| Backup (`include_sources=True`, default) | The whole project: run snapshots, job records, backups |
| Output only (`include_sources=False`) | `exports/` and `manifest.json`, nothing else |

The output-only bundle is an allowlist, so it cannot leak a prompt even if a
future release starts writing prompts into a new project-root file.

## Project migration

`project.json` carries `schema_version` (currently 2). Old documents are
migrated **lazily on read**: opening a project leaves the file byte-identical.
The first save takes a byte-for-byte backup to
`backups/project.json.<utc-stamp>.bak`, then rewrites through a temporary
sibling and an atomic replace, so an interrupted save cannot truncate the file.

The on-disk document, not the in-memory model, owns `schema_version` and
`migration_history`, so a stale caller cannot erase the audit trail.

## Preflight

A strict run is checked before the first panel is queued. Nothing is written,
downloaded, loaded, or queued during the check.

| Code | Severity | Meaning |
|---|---|---|
| `comfy.remote_not_allowed` | error | Non-loopback endpoint with `allow_remote_comfyui` off |
| `comfy.remote_without_auth` | error | Remote endpoint with no auth token configured |
| `license.not_acknowledged` | error | The profile's licence has not been accepted |
| `model.missing` / `lora.missing` | error | A declared file is not installed in ComfyUI |
| `workflow.api_graph_absent` | error | The workflow carries no graph to verify |
| `workflow.node_missing` / `workflow.input_missing` | error | A binding points at something that is not there |
| `workflow.node_class_unavailable` | error | The graph uses a node class this ComfyUI does not register |
| `reference.missing` | error | A required character reference is absent |
| `resolution.policy_invalid` | error | The profile's resolution policy contradicts itself |
| `output.unwritable` | error | The panel output directory cannot be written |
| `workflow.technical_field_unbound` | warning | The workflow does not bind `steps`/`cfg`/... so the profile cannot enforce them |
| `resolution.aspect_clamped` | warning | A panel aspect had to be clamped |
| `prompt.negative_inert_at_cfg1` | warning | The profile renders at CFG 1, where the negative prompt has no effect |

Set `comfyui.auth_token_env` in `config.yaml` to the **name** of an environment
variable holding the token. The token itself is never stored in configuration.

## Managed LM Studio (opt-in)

Disabled by default. With `lm_studio.manage_lifecycle` enabled, Manga Autopilot
loads the planner model for the run and unloads it afterwards:

```yaml
lm_studio:
  manage_lifecycle: true
  model_key: qwen3.5-9b
  identifier: manga-autopilot-planner
  ttl_seconds: 900
```

Two guarantees:

- It unloads exactly the instance it created, by identifier. A model you already
  had loaded is adopted read-only and never unloaded. `lms unload --all` is
  rejected in code, not merely avoided.
- It never downloads. `lms get` is rejected too; install the model yourself.

## Running the tests

```powershell
$env:TEMP='<a temp directory you can write to>'
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m pytest tests/backend -q
```

The whole suite is GPU-free and needs no network. Tests that would use a live
ComfyUI, a live Modal worker, real S3/R2, or a real LM Studio are opt-in through
environment variables and skip by default.

Front-end helper tests run under Node when it is installed and skip when it is
not, so a Node-less CI still runs everything else.

If pytest reports `PermissionError ... pytest-of-<user>`, point `$env:TEMP` and
`$env:TMP` at a directory the running process can write to.
