"""HTTP routes for per-panel generation (spec section 21.6).

Endpoints (all under ``/manga_autopilot/api/projects/{project_id}/panels``):

- ``POST /panels/{panel_id}/generate``   - render the panel from its current
  :class:`~manga_autopilot.models.page.PanelPlan` and prompt
- ``POST /panels/{panel_id}/regenerate`` - same as ``generate`` but allows
  the caller to override the prompt / seed / workflow_id
- ``POST /panels/{panel_id}/repair``     - re-render using the same prompt
  but bumped retries; used after a manual edit
- ``PATCH /panels/{panel_id}``           - update ``status`` / ``notes`` /
  ``image_path`` on the underlying :class:`PanelRecord`

All generation routes return the persisted :class:`GenerationJob` so the
caller can inspect the candidates and the selected one.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.models.job import JobStatus
from manga_autopilot.models.panel import (
    PanelRecord,
    load_panel_records,
    write_panel_records,
)
from manga_autopilot.services.page_editor import (
    PageEditorService,
)
from manga_autopilot.services.page_editor import (
    ProjectNotFoundError as PageEditorProjectNotFoundError,
)
from manga_autopilot.services.prompt_builder import PromptSpec

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects/{project_id}/panels"


def _storage_root(app: Application) -> Path:
    root = app.get("manga_storage_root")
    if root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    return Path(root)


def _project_root(storage_root: Path, project_id: str) -> Path:
    return storage_root / "projects" / project_id


def _find_panel(project_root: Path, panel_id: str) -> PanelRecord | None:
    panels_path = project_root / "panels.json"
    for record in load_panel_records(panels_path):
        if record.panel_id == panel_id:
            return record
    return None


def _executor(app: Application, project_id: str) -> Any:
    """Resolve a :class:`GenerationExecutor` for this project.

    Lookup order:

    1. ``app["manga_panel_executor_factory"](project_id)`` (used by tests).
    2. ``app["manga_panel_executor"]`` (a single shared executor).
    3. ``app["manga_remote_executor"]`` - a :class:`RemoteHTTPExecutor`
       configured via ``app["manga_remote_executor_settings"]``.
    4. ``app["manga_comfy_client"]`` + ``app["manga_workflow_registry"]`` -
       build a real :class:`ComfyExecutor` on the fly.
    """

    factory = app.get("manga_panel_executor_factory")
    if factory is not None:
        return factory(project_id)
    shared = app.get("manga_panel_executor")
    if shared is not None:
        return shared
    remote = app.get("manga_remote_executor")
    if remote is not None:
        return remote
    client = app.get("manga_comfy_client")
    registry = app.get("manga_workflow_registry")
    if client is None or registry is None:
        raise web.HTTPServiceUnavailable(
            text="no panel executor configured "
            "(set manga_panel_executor_factory, manga_panel_executor, "
            "manga_remote_executor, or manga_comfy_client)"
        )
    from manga_autopilot.services.generation_job import ComfyExecutor

    return ComfyExecutor(client=client, registry=registry)


def _project_service(app: Application, project_id: str) -> PageEditorService:
    return PageEditorService.for_project(_storage_root(app), project_id)


def _build_prompt(payload: dict[str, Any], panel: PanelRecord) -> PromptSpec:
    """Translate request body overrides into a :class:`PromptSpec`.

    ``payload`` may contain ``positive``, ``negative``, ``seed``, ``width``,
    ``height``, ``steps``, ``cfg`` and ``workflow_id``.  Anything not
    supplied falls back to a sensible default built from the
    :class:`PanelPlan` action/emotion strings.
    """

    action = panel.plan.action or panel.plan.purpose or "manga scene"
    emotion = panel.plan.emotion or "neutral"
    positive = str(payload.get("positive") or f"{action}, {emotion}, manga panel, lineart")
    negative = str(payload.get("negative") or "low quality, blurry")
    seed_raw = payload.get("seed")
    if isinstance(seed_raw, int):
        seed = seed_raw
    else:
        seed = abs(hash(panel.panel_id)) % (2**31)
    width = int(payload.get("width", 512))
    height = int(payload.get("height", 512))
    steps = int(payload.get("steps", 20))
    cfg = float(payload.get("cfg", 7.0))
    return PromptSpec(
        positive=positive,
        negative=negative,
        seed=seed,
        width=width,
        height=height,
        steps=steps,
        cfg=cfg,
    )


def _workflow_id(payload: dict[str, Any], app: Application) -> str:
    wid = payload.get("workflow_id")
    if isinstance(wid, str) and wid:
        return wid
    default = app.get("manga_default_workflow_id")
    if isinstance(default, str) and default:
        return default
    return "anime_t2i_default"


async def _run_generation(
    request: web.Request,
    *,
    repair: bool = False,
) -> web.Response:
    project_id = request.match_info["project_id"]
    panel_id = request.match_info["panel_id"]
    storage_root = _storage_root(request.app)
    project_root = _project_root(storage_root, project_id)

    panel = _find_panel(project_root, panel_id)
    if panel is None:
        raise web.HTTPNotFound(text=f"panel {panel_id} not found in project {project_id}")

    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    prompt = _build_prompt(body, panel)
    workflow_id = _workflow_id(body, request.app)
    page_number = int(body.get("page_number") or panel.page_number or 1)

    from manga_autopilot.services.generation_job import (
        GenerationLoop,
        GenerationLoopConfig,
    )

    max_retries = int(body.get("max_retries", 2 if repair else 1))
    threshold = float(body.get("threshold", 0.5))
    candidate_count = int(body.get("candidate_count", 1))
    loop = GenerationLoop(
        project_root=project_root,
        config=GenerationLoopConfig(
            candidate_count=max(1, candidate_count),
            max_retries=max(0, max_retries),
            threshold=threshold,
        ),
    )
    executor = _executor(request.app, project_id)
    outcome = await loop.run(
        panel=panel.plan,
        page_number=page_number,
        prompt=prompt,
        workflow_id=workflow_id,
        executor=executor,
        project_id=project_id,
    )

    _record_image_on_panel(project_root, panel_id, outcome)
    return web.json_response(
        {
            "job": outcome.job.to_dict(),
            "selected_image_path": (
                str(outcome.selected_image_path)
                if outcome.selected_image_path is not None
                else None
            ),
        },
        status=201 if not repair else 200,
    )


def _record_image_on_panel(
    project_root: Path,
    panel_id: str,
    outcome: Any,
) -> None:
    """Update the :class:`PanelRecord` for ``panel_id`` with the rendered image."""

    panels_path = project_root / "panels.json"
    records = load_panel_records(panels_path)
    selected_path = (
        str(outcome.selected_image_path)
        if outcome.selected_image_path is not None
        else None
    )
    for record in records:
        if record.panel_id != panel_id:
            continue
        record.image_path = selected_path
        record.status = (
            "generated" if outcome.job.status == JobStatus.COMPLETED else "failed"
        )
        record.history.append(
            {
                "kind": "generation",
                "job_id": outcome.job.id,
                "at": datetime.now(timezone.utc).isoformat(),
                "selected_candidate_id": outcome.job.selected_candidate_id,
                "fallback_used": outcome.job.fallback_used,
            }
        )
        record.updated_at = datetime.now(timezone.utc)
    write_panel_records(panels_path, records)


async def _payload(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


async def generate_panel(request: web.Request) -> web.Response:
    return await _run_generation(request, repair=False)


async def regenerate_panel(request: web.Request) -> web.Response:
    return await _run_generation(request, repair=False)


async def repair_panel(request: web.Request) -> web.Response:
    return await _run_generation(request, repair=True)


async def update_panel(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    panel_id = request.match_info["panel_id"]
    body = await _payload(request)

    storage_root = _storage_root(request.app)
    project_root = _project_root(storage_root, project_id)
    try:
        _project_service(request.app, project_id)
    except PageEditorProjectNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc

    panels_path = project_root / "panels.json"
    records = load_panel_records(panels_path)
    target: PanelRecord | None = None
    for record in records:
        if record.panel_id == panel_id:
            target = record
            break
    if target is None:
        raise web.HTTPNotFound(text=f"panel {panel_id} not found in project {project_id}")

    if "status" in body and isinstance(body["status"], str):
        allowed = {
            "draft",
            "queued",
            "running",
            "generated",
            "approved",
            "rejected",
            "failed",
        }
        if body["status"] not in allowed:
            raise web.HTTPBadRequest(
                text=f"status must be one of {sorted(allowed)}; got {body['status']!r}"
            )
        target.status = body["status"]
    if "notes" in body and isinstance(body["notes"], str):
        target.notes = body["notes"]
    if "image_path" in body:
        image_path = body["image_path"]
        if image_path is not None and not isinstance(image_path, str):
            raise web.HTTPBadRequest(text="image_path must be a string or null")
        target.image_path = image_path
    if "workflow_id" in body:
        target.workflow_id = body["workflow_id"]
    if "prompt_id" in body:
        target.prompt_id = body["prompt_id"]

    target.updated_at = datetime.now(timezone.utc)
    write_panel_records(panels_path, records)
    return web.json_response(target.model_dump(mode="json"))


def _load_latest_job(project_root: Path, panel_id: str) -> dict[str, Any] | None:
    """Return the most recent job JSON for ``panel_id`` (if any)."""

    jobs_dir = project_root / "jobs"
    if not jobs_dir.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for child in jobs_dir.iterdir():
        if not child.suffix == ".json":
            continue
        try:
            payload = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("panel_id") != panel_id:
            continue
        mtime = child.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, child)
    if best is None:
        return None
    return json.loads(best[1].read_text(encoding="utf-8"))


async def get_panel_status(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    panel_id = request.match_info["panel_id"]
    storage_root = _storage_root(request.app)
    project_root = _project_root(storage_root, project_id)
    panel = _find_panel(project_root, panel_id)
    if panel is None:
        raise web.HTTPNotFound(text=f"panel {panel_id} not found in project {project_id}")
    latest_job = _load_latest_job(project_root, panel_id)
    return web.json_response(
        {
            "panel": panel.model_dump(mode="json"),
            "latest_job": latest_job,
        }
    )


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_post(ROUTE_PREFIX + "/{panel_id}/generate", generate_panel)
    router.add_post(ROUTE_PREFIX + "/{panel_id}/regenerate", regenerate_panel)
    router.add_post(ROUTE_PREFIX + "/{panel_id}/repair", repair_panel)
    router.add_patch(ROUTE_PREFIX + "/{panel_id}", update_panel)
    router.add_get(ROUTE_PREFIX + "/{panel_id}", get_panel_status)


__all__ = [
    "ROUTE_PREFIX",
    "generate_panel",
    "get_panel_status",
    "regenerate_panel",
    "repair_panel",
    "register",
    "update_panel",
]
