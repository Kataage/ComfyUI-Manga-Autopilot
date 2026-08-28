"""HTTP routes for the autopilot (spec section 21.3)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.models.bible import StoryBible
from manga_autopilot.models.page import PagePlan, PanelPlan
from manga_autopilot.models.panel import (
    PanelLayout,
    PanelRecord,
    load_panel_records,
    write_panel_records,
)
from manga_autopilot.services.autopilot import (
    AutopilotController,
    AutopilotRun,
    InvalidTransitionError,
    OrchestratorHooks,
    save_run_metadata,
    start_orchestrator,
)
from manga_autopilot.services.llm_provider import (
    LLMProvider,
    LLMSettings,
    ManualProvider,
    build_provider,
)
from manga_autopilot.services.prompt_builder import PromptBuilder, PromptSpec
from manga_autopilot.storage.paths import ensure_project_paths

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects/{project_id}/autopilot"


def _controller(app: Application) -> AutopilotController:
    ctrl: Any = app.get("manga_autopilot_controller")
    if ctrl is None:
        ctrl = AutopilotController()
        app["manga_autopilot_controller"] = ctrl
    return ctrl


def _storage_root(app: Application) -> Path | None:
    root = app.get("manga_storage_root")
    if root is None:
        return None
    return Path(root)


def _llm_provider(app: Application) -> LLMProvider:
    """Return the configured LLM provider (or a safe manual one)."""

    provider = app.get("manga_llm_provider")
    if provider is not None:
        return provider
    settings = app.get("manga_llm_settings")
    if isinstance(settings, LLMSettings):
        return build_provider(settings)
    return ManualProvider(LLMSettings())


def _resolve_executor(app: Application, project_id: str) -> Any:
    """Resolve a :class:`GenerationExecutor` for ``project_id``.

    See :func:`manga_autopilot.routes.panel_routes._executor` for the
    resolution order; the autopilot hooks share the same lookup so
    the test fixture can wire in a single fake executor for both the
    autopilot start and the panel routes.
    """

    from manga_autopilot.routes.panel_routes import _executor as _panel_executor

    return _panel_executor(app, project_id)


def _default_workflow_id(app: Application) -> str:
    wid = app.get("manga_default_workflow_id")
    if isinstance(wid, str) and wid:
        return wid
    return "anime_t2i_default"


def _anima_profile(run: AutopilotRun):
    """Return the resolved profile for a strict run, or ``None`` if unknown."""
    from manga_autopilot.services.generation_profiles import load_builtin_profile

    try:
        return load_builtin_profile(str(run.input.get("generation_profile_id") or ""))
    except KeyError:
        log.warning(
            "unknown generation profile %r; falling back to generic defaults",
            run.input.get("generation_profile_id"),
        )
        return None


def _anima_candidate_count(run: AutopilotRun) -> int:
    profile = _anima_profile(run)
    return profile.candidate_count if profile is not None else 1


def _anima_technical_retry_count(run: AutopilotRun) -> int:
    profile = _anima_profile(run)
    return profile.technical_retry_count if profile is not None else 1


def _write_run_snapshot(
    app: Application,
    run: AutopilotRun,
    *,
    project_root: Path,
    project_id: str,
    profile: Any,
    workflow_id: str,
    prompts: Mapping[str, Any],
) -> None:
    """Record everything needed to reproduce this run.

    Failing to write a snapshot must never fail a run that has already produced
    images, so this logs and moves on. Model fingerprints are only included when
    ``comfyui.install_root`` is configured, since ComfyUI reports model names but
    not their paths.
    """
    from manga_autopilot.services.model_fingerprint import fingerprint_profile_models
    from manga_autopilot.services.run_snapshot import (
        EnvironmentSnapshot,
        LLMSettingsSnapshot,
        PanelPromptSnapshot,
        RunSnapshot,
        RunSnapshotWriter,
        hash_json_document,
        log_prompt_digest,
    )

    try:
        registry = app.get("manga_workflow_registry")
        workflow = None
        if registry is not None:
            try:
                workflow = registry.get(workflow_id)
            except Exception as exc:  # noqa: BLE001 - the prompts still matter
                log.warning(
                    "run snapshot has no workflow hash: %s could not be read (%s)",
                    workflow_id,
                    exc,
                )
        panels = [
            PanelPromptSnapshot.from_prompt_spec(panel_id, spec)
            for panel_id, spec in prompts.items()
        ]

        llm = None
        provider = app.get("manga_llm_provider")
        settings = getattr(provider, "settings", None)
        if settings is not None:
            llm = LLMSettingsSnapshot.from_mapping(settings.model_dump(mode="json"))

        snapshot = RunSnapshot(
            run_id=run.run_id,
            project_id=project_id,
            generation_profile_id=profile.id,
            profile_hash=hash_json_document(profile.model_dump(mode="json")),
            workflow_hash=hash_json_document((workflow.api_graph or {}) if workflow else {}),
            models=fingerprint_profile_models(profile, app.get("manga_comfyui_install_root")),
            llm=llm,
            environment=EnvironmentSnapshot.capture(),
            panels=panels,
        )
        path = RunSnapshotWriter(project_root).write(snapshot)
        for panel in panels:
            log_prompt_digest(log, panel)
        log.info("wrote %s for run %s", path.name, run.run_id)
    except Exception as exc:  # noqa: BLE001 - a snapshot is never worth a failed run
        log.warning("could not write the run snapshot for %s: %s", run.run_id, exc)


def _open_review_coordinator(app: Application, project_id: str, storage_root: Path):
    """Build the review coordinator for a run and publish it for the review API.

    A project with no Anima profile gets an empty policy, so every gate reports
    approved and the pipeline never pauses.
    """
    from manga_autopilot.routes.review_routes import register_coordinator
    from manga_autopilot.services.project_manager import ProjectManager
    from manga_autopilot.services.review_gate import (
        ReviewCoordinator,
        ReviewPolicy,
        ReviewStore,
    )

    profile_id = ""
    try:
        profile_id = ProjectManager(storage_root).load(project_id).generation_profile_id
    except Exception as exc:  # noqa: BLE001 - a missing project is not fatal here
        log.debug("could not read the generation profile for %s: %s", project_id, exc)

    root = ensure_project_paths(storage_root, project_id).root
    store = ReviewStore(root)
    coordinator = ReviewCoordinator(
        board=store.load(project_id, ReviewPolicy.for_profile(profile_id)),
        store=store,
    )
    register_coordinator(app, project_id, coordinator)
    return coordinator


def _review_coordinator(app: Application, project_id: str):
    """Return the live review coordinator for `project_id`, if a run published one."""
    from manga_autopilot.routes.review_routes import COORDINATORS_KEY

    return app.get(COORDINATORS_KEY, {}).get(project_id)


def _is_anima_run(run: AutopilotRun) -> bool:
    profile_id = run.input.get("generation_profile_id")
    return isinstance(profile_id, str) and profile_id.startswith("anima_")


async def _run_anima_preflight(
    app: Application,
    run: AutopilotRun,
    *,
    project_root: Path,
    workflow_id: str,
) -> None:
    """Block a strict Anima run whose environment is not ready.

    The check needs a live ``/object_info`` snapshot. When the application has no
    ComfyUI client - the in-process test wiring and fake executors - there is
    nothing to check against, so the gate logs and steps aside rather than
    failing a run it cannot actually assess.
    """
    from manga_autopilot.services.generation_profiles import load_builtin_profile
    from manga_autopilot.services.preflight import (
        AnimaPreflight,
        ComfyCapabilities,
        PreflightRequest,
    )

    client = app.get("manga_comfy_client")
    if client is None:
        log.warning(
            "skipping Anima preflight for %s: no ComfyUI client is configured",
            run.run_id,
        )
        return

    registry = app.get("manga_workflow_registry")
    if registry is None:
        log.warning("skipping Anima preflight for %s: no workflow registry", run.run_id)
        return

    profile = load_builtin_profile(str(run.input["generation_profile_id"]))
    workflow = registry.get(workflow_id)
    try:
        object_info = await client.get_object_info()
    except Exception as exc:  # noqa: BLE001 - re-raised with the cause named
        # "FAILED_PANEL_GENERATION" on its own sends the reader hunting through
        # prompts and workflows for a problem that is just a stopped server.
        base_url = getattr(client, "base_url", "the configured endpoint")
        raise RuntimeError(
            f"ComfyUI at {base_url} is not reachable, so nothing can be generated: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    capabilities = ComfyCapabilities.from_object_info(object_info)

    request = PreflightRequest(
        profile=profile,
        workflow=workflow,
        output_dir=project_root / "assets" / "panels",
        comfy_base_url=getattr(client, "base_url", "http://127.0.0.1:8188"),
        allow_remote_comfyui=bool(run.input.get("allow_remote_comfyui")),
        comfy_auth_configured=bool(run.input.get("comfy_auth_configured")),
        license_acknowledged=bool(run.input.get("license_acknowledged")),
    )
    report = AnimaPreflight(capabilities).run(request)
    for warning in report.warnings:
        log.warning("anima preflight: %s: %s", warning.code, warning.message)
    report.raise_if_blocked()


# ----------------------------------------------------------------- hook helpers
def _read_panel_records(project_root: Path) -> list[PanelRecord]:
    path = project_root / "panels.json"
    return load_panel_records(path)


def _write_panel_records(project_root: Path, records: list[PanelRecord]) -> None:
    path = project_root / "panels.json"
    write_panel_records(path, records)


# ----------------------------------------------------------- hook factories
def _make_validate_input() -> Callable[[AutopilotRun], dict[str, Any]]:
    def _hook(run: AutopilotRun) -> dict[str, Any]:
        idea = run.input.get("idea") or "untitled manga"
        page_count = int(run.input.get("page_count") or 1)
        language = str(run.input.get("language") or "ja")
        genre = str(run.input.get("genre") or "fantasy")
        return {"idea": idea, "page_count": page_count, "language": language, "genre": genre}

    return _hook


def _make_plan_story(
    app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    from manga_autopilot.services.story_planner import StoryPlanner

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root

    async def _hook(run: AutopilotRun) -> dict[str, Any] | None:
        try:
            from manga_autopilot.services.page_templates import layout_catalog

            strict = _is_anima_run(run)
            planner = StoryPlanner(
                provider=_llm_provider(app),
                page_count=int(run.input.get("page_count") or 1),
                language=str(run.input.get("language") or "ja"),
                genre=str(run.input.get("genre") or "fantasy"),
                strict=strict,
                # Strict planning rejects an unregistered layout id, so the
                # planner has to be told the vocabulary rather than guess it.
                layouts=layout_catalog("page") if strict else [],
            )
            plan = await planner.plan(str(run.input.get("idea") or ""))
            (project_root / "story.json").write_text(
                json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if _is_anima_run(run):
                paths.story_bible_json.write_text(
                    json.dumps(
                        plan.story_bible.model_dump(mode="json"),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            return plan.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            log.warning("plan_story failed: %s", exc)
            if _is_anima_run(run):
                raise
            return None

    return _hook


def _make_define_characters(
    app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    from manga_autopilot.services.character_planner import (
        CharacterPlanner,
        spec_to_character,
    )
    from manga_autopilot.services.character_service import CharacterService

    character_service = CharacterService.for_project(storage_root, project_id)
    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root

    async def _hook(run: AutopilotRun) -> list[str]:
        if not character_service.list():
            try:
                planner = CharacterPlanner(provider=_llm_provider(app))
                plan_input = run.artefacts.get("plan_story") or run.input
                characters = await planner.plan(
                    str(run.input.get("idea") or ""),
                    plan_input if isinstance(plan_input, (dict, str)) else {},
                )
                for ch in characters.characters:
                    # The planner emits a CharacterSpec (free text plus traits);
                    # the persisted card needs a structured Character.
                    character_service.create(spec_to_character(ch))
            except Exception as exc:  # noqa: BLE001
                if _is_anima_run(run):
                    # A strict run without characters fails later anyway, with a
                    # confusing "character is not defined" from panel planning.
                    # Fail here, where the cause is still visible.
                    raise
                log.warning("define_characters failed: %s", exc)
        ids = [c.id for c in character_service.list()]
        if project_root.joinpath("characters.json").exists():
            (project_root / "characters.json").write_text(
                json.dumps([c.model_dump(mode="json") for c in character_service.list()], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return ids

    return _hook


def _make_plan_pages(
    app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    from manga_autopilot.services.page_planner import PagePlanner

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root

    async def _hook(run: AutopilotRun) -> list[PagePlan]:
        page_count = int(run.input.get("page_count") or 1)
        strict = _is_anima_run(run)
        try:
            story_plan = run.artefacts.get("plan_story")
            if not isinstance(story_plan, (dict, str)):
                story_plan = {"title": "", "pages": []}
            if strict:
                if not isinstance(story_plan, dict):
                    raise ValueError("strict planning requires a structured StoryPlan")
                pages = [PagePlan.model_validate(item) for item in story_plan.get("pages", [])]
                if len(pages) != page_count:
                    raise ValueError(
                        f"StoryPlan contains {len(pages)} pages; expected {page_count}"
                    )
                from manga_autopilot.services.page_templates import (
                    fallback_grid,
                    layout_catalog,
                )

                layout_slots = {
                    str(item["layout_id"]): int(item["panel_count"])
                    for item in layout_catalog("page")
                }
                for page in pages:
                    if page.layout_id is None:
                        page.layout_id = fallback_grid(page.panel_count).template_id
                    elif page.layout_id.startswith("fallback_grid_"):
                        expected = fallback_grid(page.panel_count).template_id
                        if page.layout_id != expected:
                            raise ValueError(
                                f"layout {page.layout_id!r} does not match "
                                f"panel_count={page.panel_count}"
                            )
                    elif page.layout_id not in layout_slots:
                        raise ValueError(f"layout {page.layout_id!r} is not registered")
                    elif layout_slots[page.layout_id] != page.panel_count:
                        raise ValueError(
                            f"layout {page.layout_id!r} has "
                            f"{layout_slots[page.layout_id]} slots; "
                            f"received panel_count={page.panel_count}"
                        )
            else:
                planner = PagePlanner(provider=_llm_provider(app), page_count=page_count)
                plan_list = await planner.plan(story_plan)
                pages = list(plan_list.pages)
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise
            log.warning("plan_pages fell back to stub: %s", exc)
            pages = [
                PagePlan(
                    page_number=i + 1,
                    summary=f"page {i + 1}",
                    panel_count=int(run.input.get("panels_per_page") or 1),
                )
                for i in range(page_count)
            ]
        (project_root / "pages.json").write_text(
            json.dumps([p.model_dump(mode="json") for p in pages], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return [p.model_dump(mode="json") for p in pages]

    return _hook


def _make_plan_panels(
    app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    from manga_autopilot.services.panel_planner import PanelPlanner

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root

    async def _hook(run: AutopilotRun) -> list[dict[str, Any]]:
        from manga_autopilot.models.scene_state import SceneState
        from manga_autopilot.services.character_service import CharacterService
        from manga_autopilot.services.page_templates import fallback_grid, layout_catalog
        from manga_autopilot.services.scene_state_reducer import apply_scene_delta

        existing = {r.panel_id: r for r in _read_panel_records(project_root)}
        panels_per_page = int(run.input.get("panels_per_page") or 1)
        strict = _is_anima_run(run)
        records: list[PanelRecord] = []
        raw_pages: list[Any] = run.artefacts.get("plan_pages") or []
        pages: list[PagePlan] = []
        for item in raw_pages:
            if isinstance(item, PagePlan):
                pages.append(item)
            elif isinstance(item, dict):
                try:
                    pages.append(PagePlan.model_validate(item))
                except Exception:
                    continue
        if not pages:
            if strict:
                raise ValueError(
                    "no page plans are available; page planning produced nothing"
                )
            return [r.model_dump(mode="json") for r in existing.values()]

        story_payload = run.artefacts.get("plan_story")
        story_bible = StoryBible()
        if isinstance(story_payload, dict):
            story_bible = StoryBible.model_validate(
                story_payload.get("story_bible")
                or story_payload.get("storyBible")
                or {}
            )
        character_service = CharacterService.for_project(storage_root, project_id)
        characters = character_service.list()
        known_character_ids = {character.id for character in characters}
        character_payloads = [
            character.model_dump(mode="json") for character in characters
        ]
        state = SceneState()
        if paths.scene_state_json.exists():
            state = SceneState.model_validate_json(
                paths.scene_state_json.read_text(encoding="utf-8")
            )

        for page in pages:
            effective_count = page.panel_count if strict else panels_per_page
            page_layout_id = page.layout_id
            if strict and page_layout_id is None:
                page_layout_id = fallback_grid(effective_count).template_id
                page.layout_id = page_layout_id
            catalog = layout_catalog("page")
            registered_layout_ids = {
                str(item["layout_id"]) for item in catalog
            }
            if page_layout_id and page_layout_id.startswith("fallback_grid_"):
                registered_layout_ids.add(page_layout_id)
                catalog.append(
                    {
                        "layout_id": page_layout_id,
                        "panel_count": effective_count,
                        "reading_order": list(range(1, effective_count + 1)),
                    }
                )
            planner = PanelPlanner(
                provider=_llm_provider(app),
                panel_count=effective_count,
                strict=strict,
                character_ids=known_character_ids,
                layout_id=page_layout_id,
                registered_layout_ids=registered_layout_ids,
                story_bible=story_bible.model_dump(mode="json"),
                scene_state=state.model_dump(mode="json"),
                active_characters=character_payloads,
                layouts=catalog,
            )
            try:
                panel_list = await planner.plan(page)
                plans = list(panel_list.panels)[:effective_count]
            except Exception as exc:  # noqa: BLE001
                if strict:
                    raise
                log.warning("plan_panels fell back to stub for page %s: %s", page.page_number, exc)
                plans = []
            while len(plans) < effective_count:
                pn = len(plans) + 1
                plans.append(
                    PanelPlan(
                        panel_number=pn,
                        purpose=f"panel {pn} of page {page.page_number}",
                        action=page.summary or "scene",
                    )
                )
            for plan in plans:
                if strict:
                    reduced = apply_scene_delta(
                        state,
                        plan.scene_delta,
                        known_character_ids,
                    )
                    if not reduced.applied:
                        details = "; ".join(item.message for item in reduced.warnings)
                        raise ValueError(f"scene delta rejected: {details}")
                    state = reduced.state
                    paths.scene_state_json.write_text(
                        json.dumps(
                            state.model_dump(mode="json"),
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                pid = f"panel_{page.page_number:03d}_{plan.panel_number:02d}"
                record = existing.get(pid) or PanelRecord(
                    panel_id=pid,
                    page_number=page.page_number,
                    plan=plan,
                )
                record.plan = plan
                record.page_number = page.page_number
                record.updated_at = datetime.now(timezone.utc)
                records.append(record)
        _write_panel_records(project_root, records)
        return [r.model_dump(mode="json") for r in records]

    return _hook


def _make_generate_panels(
    app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    from manga_autopilot.services.generation_job import (
        GenerationLoop,
        GenerationLoopConfig,
        run_panels_sequentially,
    )

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root
    workflow_id = _default_workflow_id(app)
    cancel_json_path = paths.cancel_json

    def _cancel_check() -> bool:
        """Check if a cancel marker exists on disk."""
        if cancel_json_path.exists():
            return True
        # Also check in-memory cancel event from controller
        ctrl = _controller(app)
        run_data = ctrl.runs.get(project_id)
        if run_data is not None and run_data.cancel_event is not None:
            return run_data.cancel_event.is_set()
        return False

    async def _hook(run: AutopilotRun) -> list[dict[str, Any]]:
        records = _read_panel_records(project_root)
        if not records:
            return []
        strict = _is_anima_run(run)
        if strict:
            await _run_anima_preflight(
                app,
                run,
                project_root=project_root,
                workflow_id=str(run.input.get("workflow_id") or workflow_id),
            )
        executor = _resolve_executor(app, project_id)
        loop = GenerationLoop(
            project_root=project_root,
            config=GenerationLoopConfig(
                candidate_count=_anima_candidate_count(run) if strict else (
                    int(run.input["candidate_count"]) if run.input.get("candidate_count") is not None else 1
                ),
                max_retries=int(run.input["max_retries"]) if run.input.get("max_retries") is not None else 1,
                threshold=float(run.input["threshold"]) if run.input.get("threshold") is not None else 0.5,
                strict=strict,
                technical_retry_count=_anima_technical_retry_count(run) if strict else 1,
            ),
        )
        builder = PromptBuilder(provider=_llm_provider(app))

        if strict:
            # A strict run persists each panel before submitting the next, so an
            # interruption resumes instead of re-rendering, and it never
            # substitutes a fabricated prompt for one the planner could not build.
            #
            # Prompts are rendered deterministically from the panel plan and the
            # profile - no LLM call, and no technical field the profile does not
            # own. That is the whole point of the Anima profiles.
            from manga_autopilot.services.anima_prompt_builder import (
                AnimaPromptBuilder,
                panel_aspect,
                segments_from_panel_plan,
                stable_panel_seed,
            )
            from manga_autopilot.services.character_service import CharacterService
            from manga_autopilot.services.review_gate import run_with_early_artwork_review

            profile = _anima_profile(run)
            if profile is None:
                raise RuntimeError(
                    f"strict run {run.run_id} has no resolvable generation profile"
                )
            anima_builder = AnimaPromptBuilder()
            characters = {c.id: c for c in CharacterService.for_project(storage_root, project_id).list()}
            base_seed = run.input.get("seed")

            rendered_prompts: dict[str, PromptSpec] = {}

            def _anima_prompt(record: PanelRecord) -> PromptSpec:
                seed = (
                    int(base_seed)
                    if base_seed is not None
                    else stable_panel_seed(project_id, record.panel_id)
                )
                spec = anima_builder.render(
                    segments_from_panel_plan(record.plan, characters),
                    profile,
                    seed=seed,
                    panel_size=panel_aspect(record, profile),
                )
                rendered_prompts[record.panel_id] = spec
                return spec

            async def _generate(batch: list[PanelRecord]) -> list[Any]:
                return await run_panels_sequentially(
                    records=batch,
                    loop=loop,
                    executor=executor,
                    prompt_for=_anima_prompt,
                    workflow_id=str(run.input.get("workflow_id") or workflow_id),
                    project_id=project_id,
                    persist=lambda _batch: _write_panel_records(project_root, records),
                    run_id=run.run_id,
                    cancel_check=_cancel_check,
                )

            coordinator = _review_coordinator(app, project_id)
            if coordinator is None:
                results = await _generate(records)
            else:
                # Page one is rendered on its own so the artwork direction can be
                # judged before the remaining pages consume GPU time.
                results = await run_with_early_artwork_review(
                    records,
                    generate=_generate,
                    coordinator=coordinator,
                    on_wait=lambda gate: run.log_event("review_awaiting", {"gate": gate}),
                )
            _write_run_snapshot(
                app,
                run,
                project_root=project_root,
                project_id=project_id,
                profile=profile,
                workflow_id=str(run.input.get("workflow_id") or workflow_id),
                prompts=rendered_prompts,
            )

            blocked = [r for r in results if r.status in {"failed", "rejected"}]
            if blocked:
                raise RuntimeError(
                    "strict generation stopped at "
                    + ", ".join(f"{r.panel_id} ({r.status})" for r in blocked)
                )
            return [r.outcome.job.to_dict() for r in results if r.outcome is not None]

        rendered: list[dict[str, Any]] = []
        failed_panel_ids: list[str] = []
        skipped_panel_ids: list[str] = []
        for record in records:
            # Idempotent: skip panels that are already generated.
            if (
                record.image_path is not None
                and record.status == "generated"
                and Path(record.image_path).exists()
            ):
                skipped_panel_ids.append(record.panel_id)
                continue
            try:
                try:
                    prompt = await builder.build(record.plan)
                except Exception as exc:  # noqa: BLE001
                    log.warning("PromptBuilder.build failed (%s); using fallback", exc)
                    prompt = PromptSpec(
                        positive=record.plan.action or "manga panel",
                        negative="low quality",
                        seed=abs(hash(record.panel_id)) % (2**31),
                        width=512,
                        height=512,
                        steps=20,
                        cfg=7.0,
                    )
                outcome = await loop.run(
                    panel=record.plan,
                    page_number=record.page_number,
                    prompt=prompt,
                    workflow_id=str(run.input.get("workflow_id") or workflow_id),
                    executor=executor,
                    project_id=project_id,
                    cancel_check=_cancel_check,
                    run_id=run.run_id,
                )
                if outcome.selected_image_path is not None:
                    record.image_path = str(outcome.selected_image_path)
                if outcome.job.status.value == "completed":
                    record.status = "generated"
                else:
                    record.status = "failed"
                    failed_panel_ids.append(record.panel_id)
                record.history.append(
                    {
                        "kind": "autopilot_generation",
                        "job_id": outcome.job.id,
                        "at": datetime.now(timezone.utc).isoformat(),
                        "selected_candidate_id": outcome.job.selected_candidate_id,
                    }
                )
                record.updated_at = datetime.now(timezone.utc)
                rendered.append(outcome.job.to_dict())
            except Exception as exc:  # noqa: BLE001
                log.warning("generate_panels failed for %s: %s", record.panel_id, exc)
                record.status = "failed"
                record.updated_at = datetime.now(timezone.utc)
                failed_panel_ids.append(record.panel_id)
        _write_panel_records(project_root, records)
        if failed_panel_ids:
            raise RuntimeError(
                f"panel generation failed for: {', '.join(failed_panel_ids)}"
            )
        return rendered

    return _hook


def _make_qa_panels(
    _app: Application,
    _project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    paths = ensure_project_paths(storage_root, _project_id)
    project_root = paths.root

    def _hook(run: AutopilotRun) -> dict[str, int]:
        records = _read_panel_records(project_root)
        total = len(records)
        passed = sum(1 for r in records if r.status == "generated")
        return {"passed": passed, "total": total}

    return _hook


def _make_lettering(
    _app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], dict[str, Any]]:
    from manga_autopilot.models.bubble import SpeechBubble
    from manga_autopilot.services.bubble_layout import place_bubbles
    from manga_autopilot.services.bubble_service import BubbleService

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root
    service = BubbleService(project_root=project_root)

    def _hook(run: AutopilotRun) -> dict[str, Any]:
        records = _read_panel_records(project_root)
        bubble_count = 0

        for record in records:
            dialogues = record.plan.dialogue if record.plan else []
            bubbles: list[SpeechBubble] = []

            if dialogues:
                for idx, dlg in enumerate(dialogues):
                    # Map Dialogue.type to BubbleType
                    bubble_type_map = {
                        "speech": "normal",
                        "thought": "thought",
                        "narration": "narration",
                        "whisper": "whisper",
                    }
                    bubble_type = bubble_type_map.get(dlg.type, "normal")
                    bubble = SpeechBubble(
                        id=f"{record.panel_id}_b{idx:02d}",
                        panel_id=record.panel_id,
                        type=bubble_type,
                        text=dlg.text,
                        width=160.0,
                        height=80.0,
                        order=idx,
                    )
                    bubbles.append(bubble)
            else:
                # Fallback: at least one bubble so the page is never bare.
                bubble = SpeechBubble(
                    id=f"{record.panel_id}_b00",
                    panel_id=record.panel_id,
                    type="normal",
                    text="行くぞ",
                    width=160.0,
                    height=80.0,
                    order=0,
                )
                bubbles.append(bubble)

            # Compute placements using the panel layout (if available).
            panel_layout = record.layout
            if panel_layout is None:
                panel_layout = PanelLayout(
                    panel_id=record.panel_id,
                    x=0,
                    y=0,
                    width=512,
                    height=512,
                )
            placements = place_bubbles(bubbles, panel_layout)
            for placement in placements:
                # Persist the placed position back onto the bubble.
                b = placement.bubble
                b.x = placement.x
                b.y = placement.y
                b.width = placement.width
                b.height = placement.height
                service.upsert(b)
                bubble_count += 1

        return {
            "bubbles_path": str(service.bubbles_path),
            "bubble_count": bubble_count,
        }

    return _hook


# Page dimensions used by export_page_png (spec section 20.1).
_PAGE_WIDTH = 1200
_PAGE_HEIGHT = 1600


def _assign_fallback_layouts(
    records: list[PanelRecord],
    page_number: int,
) -> list[PanelLayout]:
    """Assign non-overlapping :class:`PanelLayout` for panels on one page.

    Panels with an explicit ``record.layout`` keep it; the others receive
    a computed layout so that all panels on the same page are visible and
    do not overlap.  The layout follows a simple grid strategy:

    * 1 panel  → full page (with 4 px outer margin)
    * 2 panels → top / bottom halves
    * 3 panels → top half / bottom-left / bottom-right
    * 4+ panels → 2xN grid

    The ``image_path`` is *not* set here; the caller does that.
    """

    n = len(records)
    if n == 0:
        return []

    # If every record already has an explicit layout, just return them.
    if all(r.layout is not None for r in records):
        return [r.layout for r in records]  # type: ignore[misc]

    margin = 4
    pw = _PAGE_WIDTH - 2 * margin
    ph = _PAGE_HEIGHT - 2 * margin

    def _make(x: float, y: float, w: float, h: float, pid: str) -> PanelLayout:
        return PanelLayout(panel_id=pid, x=x, y=y, width=w, height=h)

    layouts: list[PanelLayout] = []
    for idx, record in enumerate(records):
        if record.layout is not None:
            layouts.append(record.layout)
            continue
        pid = record.panel_id
        if n == 1:
            layouts.append(_make(margin, margin, pw, ph, pid))
        elif n == 2:
            if idx == 0:
                layouts.append(_make(margin, margin, pw, ph / 2, pid))
            else:
                layouts.append(_make(margin, margin + ph / 2, pw, ph / 2, pid))
        elif n == 3:
            if idx == 0:
                layouts.append(_make(margin, margin, pw, ph / 2, pid))
            elif idx == 1:
                layouts.append(_make(margin, margin + ph / 2, pw / 2, ph / 2, pid))
            else:
                layouts.append(_make(margin + pw / 2, margin + ph / 2, pw / 2, ph / 2, pid))
        else:
            # 4+ panels → 2-column grid
            cols = 2
            rows = (n + cols - 1) // cols
            cell_w = pw / cols
            cell_h = ph / rows
            row = idx // cols
            col = idx % cols
            layouts.append(_make(
                margin + col * cell_w,
                margin + row * cell_h,
                cell_w,
                cell_h,
                pid,
            ))
    return layouts


def _make_render_page(
    _app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    from manga_autopilot.services.bubble_layout import place_bubbles
    from manga_autopilot.services.bubble_renderer import draw_bubble_on_canvas
    from manga_autopilot.services.bubble_service import BubbleService
    from manga_autopilot.services.export import export_page_png

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root
    bubble_service = BubbleService(project_root=project_root)

    def _overlay_bubbles_on_page(
        page_path: Path,
        panel_entries: list[tuple[PanelLayout, str]],
    ) -> int:
        from PIL import Image

        if not page_path.exists():
            return 0

        total_bubbles = 0
        try:
            with Image.open(page_path) as img:
                canvas = img.convert("RGBA")
                for layout, panel_id in panel_entries:
                    bubbles = bubble_service.list_bubbles(panel_id=panel_id)
                    if not bubbles:
                        continue
                    placements = place_bubbles(bubbles, layout)
                    for placement in placements:
                        draw_bubble_on_canvas(
                            canvas,
                            placement.bubble,
                            placement.x,
                            placement.y,
                            placement.width,
                            placement.height,
                        )
                        total_bubbles += 1
                canvas.convert("RGB").save(page_path, format="PNG")
        except Exception as exc:  # noqa: BLE001
            log.warning("bubble overlay failed for %s: %s", page_path, exc)
        return total_bubbles

    def _hook(run: AutopilotRun) -> list[dict[str, Any]]:
        records = _read_panel_records(project_root)
        # Group records by page, then assign non-overlapping fallback layouts.
        by_page: dict[int, list] = {}
        for record in records:
            if record.image_path is None:
                continue
            by_page.setdefault(record.page_number, []).append(record)
        pages: dict[int, list[tuple[PanelLayout, str]]] = {}
        for page_number, page_records in by_page.items():
            layouts = _assign_fallback_layouts(page_records, page_number)
            for record, layout in zip(page_records, layouts, strict=True):
                layout.image_path = record.image_path
                pages.setdefault(page_number, []).append((layout, record.panel_id))
        rendered: list[dict[str, Any]] = []
        for page_number, panel_entries in sorted(pages.items()):
            page_id = f"page_{page_number:04d}"
            try:
                panels_for_export = [entry[0] for entry in panel_entries]
                out_path = export_page_png(project_root, page_id, panels_for_export)
                bubble_count = _overlay_bubbles_on_page(out_path, panel_entries)
                rendered.append({
                    "page": page_number,
                    "path": str(out_path),
                    "panel_count": len(panel_entries),
                    "bubble_count": bubble_count,
                })
            except Exception as exc:  # noqa: BLE001
                log.warning("render_page failed for page %s: %s", page_number, exc)
        return rendered

    return _hook


def _make_export(
    _app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    from manga_autopilot.services.export import ExportService

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root
    service = ExportService(storage_root=storage_root)

    def _hook(_run: AutopilotRun) -> dict[str, Any]:
        result: dict[str, Any] = {
            "pages": [],
            "webtoon": [],
            "pdf": None,
            "project_root": str(project_root),
        }
        try:
            # Collect page PNGs in sorted order.
            pages_dir = paths.export("pages")
            page_pngs = sorted(p for p in pages_dir.glob("page_*.png") if p.is_file())
            result["pages"] = [str(p) for p in page_pngs]

            # Generate webtoon if we have page PNGs.
            if page_pngs:
                try:
                    webtoon_paths = service.webtoon(project_id, page_pngs)
                    result["webtoon"] = [str(p) for p in webtoon_paths]
                except Exception as exc:  # noqa: BLE001
                    log.warning("webtoon export failed: %s", exc)

                # Generate PDF.
                try:
                    pdf_path = service.pdf(project_id, page_pngs)
                    result["pdf"] = str(pdf_path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("pdf export failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("export failed: %s", exc)

        return result

    return _hook


def _make_finalize(
    _app: Application,
    project_id: str,
    storage_root: Path,
) -> Callable[[AutopilotRun], Any]:
    from manga_autopilot.services.autopilot import (
        ManifestExports,
        ManifestStats,
        ManifestWriter,
    )

    paths = ensure_project_paths(storage_root, project_id)
    project_root = paths.root
    service_for_exports = _make_export(_app, project_id, storage_root)

    def _hook(run: AutopilotRun) -> str:
        exports_info = service_for_exports(run)
        pages: list[str] = exports_info.get("pages", [])  # type: ignore[arg-type]
        webtoon: list[str] = exports_info.get("webtoon", [])  # type: ignore[arg-type]
        pdf: str | None = exports_info.get("pdf")  # type: ignore[assignment]
        records = _read_panel_records(project_root)
        generated_images = sum(1 for r in records if r.image_path)
        try:
            writer = ManifestWriter(project_root)
            writer.write(
                project_id=project_id,
                run_id=run.run_id,
                title=str(run.input.get("title") or project_id),
                status="completed",
                created_at=run.started_at.isoformat(),
                completed_at=(run.finished_at or datetime.now(timezone.utc)).isoformat(),
                exports=ManifestExports(pages=pages, webtoon=webtoon, pdf=pdf),
                stats=ManifestStats(
                    page_count=int(run.input.get("page_count") or 1),
                    panel_count=len(records),
                    generated_images=generated_images,
                    regenerated_panels=0,
                    average_qa_score=0.0,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("finalize failed: %s", exc)
        return str(project_root / "manifest.json")

    return _hook


def _default_hooks_for_project(
    app: Application,
    project_id: str,
    storage_root: Path,
) -> OrchestratorHooks:
    """Build a hooks object wired to the real services for ``project_id``.

    The hooks read providers / executors from ``app``:

    - ``app["manga_llm_provider"]`` -- LLM (falls back to a manual no-op)
    - ``app["manga_panel_executor_factory"]`` -- :class:`GenerationExecutor`
    - ``app["manga_default_workflow_id"]`` -- override default workflow

    Each hook degrades gracefully when its upstream is missing so the
    autopilot can be exercised end-to-end in tests without a network.
    """

    return OrchestratorHooks(
        validate_input=_make_validate_input(),
        plan_story=_make_plan_story(app, project_id, storage_root),
        define_characters=_make_define_characters(app, project_id, storage_root),
        plan_pages=_make_plan_pages(app, project_id, storage_root),
        plan_panels=_make_plan_panels(app, project_id, storage_root),
        build_prompts=lambda run: {"style": run.input.get("style", "manga")},
        validate_workflow=lambda run: list(
            w.workflow_id for w in (app.get("manga_workflow_registry") or type("R", (), {"list": lambda self: []})()).list()
        ),
        generate_panels=_make_generate_panels(app, project_id, storage_root),
        qa_panels=_make_qa_panels(app, project_id, storage_root),
        lettering=_make_lettering(app, project_id, storage_root),
        render_pages=_make_render_page(app, project_id, storage_root),
        export=_make_export(app, project_id, storage_root),
        finalize=_make_finalize(app, project_id, storage_root),
    )


async def _payload(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


async def start(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body: dict[str, Any] = {}
    if request.body_exists or request.content_length:
        try:
            body = await _payload(request)
        except web.HTTPBadRequest:
            body = {}
    input_payload = body.get("input") if isinstance(body.get("input"), dict) else body
    workflow_id = body.get("workflow_id") or input_payload.get("workflow_id")
    if workflow_id:
        input_payload = {**input_payload, "workflow_id": workflow_id}

    storage_root = _storage_root(request.app)
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")

    ensure_project_paths(storage_root, project_id)

    ctrl = _controller(request.app)
    hooks = _default_hooks_for_project(request.app, project_id, storage_root)
    run, _task, _cancel, _pause = start_orchestrator(
        ctrl,
        project_id,
        hooks=hooks,
        project_root=ensure_project_paths(storage_root, project_id).root,
        input_payload=input_payload,
        reviews=_open_review_coordinator(request.app, project_id, storage_root),
    )
    save_run_metadata(ensure_project_paths(storage_root, project_id).root, run)
    return web.json_response(run.to_status(), status=202)


async def pause(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        run = ctrl.pause(project_id)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(run.to_status())


async def resume(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        run = ctrl.resume(project_id)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(run.to_status())


async def cancel(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    storage_root = _storage_root(request.app)
    try:
        body = await _payload(request)
    except web.HTTPBadRequest:
        body = {}
    reason = body.get("reason", "user_cancelled") if isinstance(body, dict) else "user_cancelled"

    try:
        run = ctrl.cancel(project_id, reason=reason)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc

    # Write cancel marker to disk so GenerationLoop can detect it
    if storage_root is not None:
        import json as _json
        from datetime import datetime, timezone

        from manga_autopilot.services.run_artifacts import (
            inject_artifacts_root_to_manifest,
            mirror_latest_artifacts_to_run,
            read_run_artifacts_summary,
        )
        from manga_autopilot.storage.paths import ensure_project_paths

        paths = ensure_project_paths(storage_root, project_id)
        cancel_marker = {
            "requested": True,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }
        paths.cancel_json.write_text(_json.dumps(cancel_marker, indent=2))
        try:
            save_run_metadata(paths.root, run, update_latest=False)
        except Exception:  # noqa: BLE001
            pass

        # Mirror partial artifacts into run directory on cancel.
        try:
            mirror_latest_artifacts_to_run(paths.root, run.run_id)
            artifacts_summary = read_run_artifacts_summary(paths.root, run.run_id)
            save_run_metadata(paths.root, run, artifacts=artifacts_summary, update_latest=False)
            inject_artifacts_root_to_manifest(paths.root, run.run_id)
        except Exception:  # noqa: BLE001
            pass

    status_data = run.to_status()
    status_data["cancel_marker"] = {"requested": True, "reason": reason}
    return web.json_response(status_data)


async def status(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        snapshot = ctrl.status(project_id)
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(snapshot)


async def restart(request: web.Request) -> web.Response:
    """Restart a cancelled autopilot run as a fresh run.

    Clears the cancel marker and starts a new Autopilot pipeline using
    the existing project configuration.  Partial assets from the
    previous run may be overwritten.
    """
    project_id = request.match_info["project_id"]
    storage_root = _storage_root(request.app)
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")

    paths = ensure_project_paths(storage_root, project_id)
    if not paths.project_json.exists():
        raise web.HTTPNotFound(text=f"project {project_id!r} not found")

    # Read override inputs from request body
    try:
        body = await _payload(request)
    except web.HTTPBadRequest:
        body = {}
    overrides = body if isinstance(body, dict) else {}

    # Restore input from project.json
    import json as _json
    project_data = _json.loads(paths.project_json.read_text(encoding="utf-8"))
    restored_input: dict[str, Any] = {}

    # Restore from project settings
    settings = project_data.get("settings", {})
    gen_settings = settings.get("generation", {})
    restored_input["page_count"] = settings.get("page_count", 4)
    restored_input["candidate_count"] = gen_settings.get("candidate_count", 1)
    restored_input["max_retries"] = gen_settings.get("max_retry_per_panel", 1)
    restored_input["threshold"] = gen_settings.get("quality_threshold", 0.5)
    restored_input["title"] = project_data.get("title", project_data.get("name", project_id))

    # Apply overrides
    restored_input.update(overrides)

    # Read previous run_id from latest_run_id.txt
    previous_run_id: str | None = None
    latest_run_id_path = paths.latest_run_id_txt
    if latest_run_id_path.exists():
        previous_run_id = latest_run_id_path.read_text(encoding="utf-8").strip() or None

    # Clear cancel marker
    if paths.cancel_json.exists():
        paths.cancel_json.unlink()

    # Start fresh autopilot run
    ctrl = _controller(request.app)
    hooks = _default_hooks_for_project(request.app, project_id, storage_root)
    run, _task, _cancel, _pause = start_orchestrator(
        ctrl,
        project_id,
        hooks=hooks,
        project_root=paths.root,
        input_payload=restored_input,
        reviews=_open_review_coordinator(request.app, project_id, storage_root),
    )
    # Link to previous run
    run.source["restart_of_run_id"] = previous_run_id
    save_run_metadata(paths.root, run)
    return web.json_response({
        "project_id": project_id,
        "run_id": run.run_id,
        "status": "started",
        "restarted": True,
    }, status=202)


RESUME_CANCELLED_ROUTE = "/manga_autopilot/api/projects/{project_id}/autopilot/resume-cancelled"


async def resume_cancelled(request: web.Request) -> web.Response:
    """Resume a cancelled or failed autopilot run, reusing existing panels.

    Clears the cancel marker and starts a new Autopilot pipeline that
    preserves already-generated panel images and only regenerates missing
    or broken ones.
    """
    project_id = request.match_info["project_id"]
    storage_root = _storage_root(request.app)
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")

    paths = ensure_project_paths(storage_root, project_id)
    if not paths.project_json.exists():
        raise web.HTTPNotFound(text=f"project {project_id!r} not found")

    # Read override inputs from request body
    try:
        body = await _payload(request)
    except web.HTTPBadRequest:
        body = {}
    overrides = body if isinstance(body, dict) else {}

    # Restore input from project.json
    import json as _json
    project_data = _json.loads(paths.project_json.read_text(encoding="utf-8"))
    restored_input: dict[str, Any] = {}

    # Restore from project settings
    settings = project_data.get("settings", {})
    gen_settings = settings.get("generation", {})
    restored_input["page_count"] = settings.get("page_count", 4)
    restored_input["candidate_count"] = gen_settings.get("candidate_count", 1)
    restored_input["max_retries"] = gen_settings.get("max_retry_per_panel", 1)
    restored_input["threshold"] = gen_settings.get("quality_threshold", 0.5)
    restored_input["title"] = project_data.get("title", project_data.get("name", project_id))

    # Apply overrides
    restored_input.update(overrides)

    # Read previous run_id from latest_run_id.txt
    previous_run_id: str | None = None
    latest_run_id_path = paths.latest_run_id_txt
    if latest_run_id_path.exists():
        previous_run_id = latest_run_id_path.read_text(encoding="utf-8").strip() or None

    # Clear cancel marker
    if paths.cancel_json.exists():
        paths.cancel_json.unlink()

    # Start new autopilot run (panels.json is preserved, so existing
    # generated panels are reused and only missing ones are regenerated)
    ctrl = _controller(request.app)
    hooks = _default_hooks_for_project(request.app, project_id, storage_root)
    run, _task, _cancel, _pause = start_orchestrator(
        ctrl,
        project_id,
        hooks=hooks,
        project_root=paths.root,
        input_payload=restored_input,
        reviews=_open_review_coordinator(request.app, project_id, storage_root),
    )
    # Link to previous run as resume
    run.source["resume_of_run_id"] = previous_run_id
    save_run_metadata(paths.root, run)
    return web.json_response({
        "project_id": project_id,
        "run_id": run.run_id,
        "status": "started",
        "resumed": True,
        "resume_of_run_id": previous_run_id,
    }, status=202)


CLEANUP_ROUTE = "/manga_autopilot/api/projects/{project_id}/runs/cleanup"


async def cleanup_runs(request: web.Request) -> web.Response:
    """Dry-run or execute cleanup of old per-run artifact directories.

    Default is dry_run=true.  Only deletes when dry_run=false.
    Latest run and running runs are protected by default.
    """
    project_id = request.match_info["project_id"]
    storage_root = _storage_root(request.app)
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")

    paths = ensure_project_paths(storage_root, project_id)
    if not paths.project_json.exists():
        raise web.HTTPNotFound(text=f"project {project_id!r} not found")

    try:
        body = await _payload(request)
    except web.HTTPBadRequest:
        body = {}

    from manga_autopilot.services.run_cleanup import (
        RunCleanupPlan,
        RunCleanupPolicy,
        build_run_cleanup_plan,
        execute_run_cleanup_plan,
    )

    policy = RunCleanupPolicy(
        keep_latest=True,
        keep_last=int(body.get("keep_last", 5)),
        delete_completed=bool(body.get("delete_completed", True)),
        delete_failed=bool(body.get("delete_failed", True)),
        delete_cancelled=bool(body.get("delete_cancelled", True)),
        delete_running=bool(body.get("delete_running", False)),
        dry_run=bool(body.get("dry_run", True)),
    )

    plan = build_run_cleanup_plan(paths.root, policy)
    plan = RunCleanupPlan(
        project_id=project_id,
        dry_run=plan.dry_run,
        protected_run_ids=plan.protected_run_ids,
        candidates=plan.candidates,
    )

    result = execute_run_cleanup_plan(plan)

    return web.json_response({
        "project_id": project_id,
        "dry_run": result.dry_run,
        "protected_run_ids": plan.protected_run_ids,
        "candidates": [
            {
                "run_id": c.run_id,
                "status": c.status,
                "path": Path(c.path).relative_to(paths.root).as_posix()
                if Path(c.path).is_relative_to(paths.root)
                else c.path,
                "reason": c.reason,
            }
            for c in plan.candidates
        ],
        "deleted_run_ids": result.deleted_run_ids,
        "skipped_run_ids": result.skipped_run_ids,
        "errors": result.errors,
    })


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_post(ROUTE_PREFIX + "/start", start)
    router.add_post(ROUTE_PREFIX + "/pause", pause)
    router.add_post(ROUTE_PREFIX + "/resume", resume)
    router.add_post(ROUTE_PREFIX + "/cancel", cancel)
    router.add_post(ROUTE_PREFIX + "/restart", restart)
    router.add_post(RESUME_CANCELLED_ROUTE, resume_cancelled)
    router.add_get(ROUTE_PREFIX + "/status", status)
    router.add_post(CLEANUP_ROUTE, cleanup_runs)


__all__ = ["register", "ROUTE_PREFIX"]
