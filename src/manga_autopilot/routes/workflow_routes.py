"""HTTP routes for the workflow registry (spec section 21.5)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.models.workflow import (
    WorkflowValidationError,
    validate_api_graph,
)
from manga_autopilot.services.comfy_client import (
    ComfyClient,
    ComfyUIError,
    ComfyUIRequestError,
)
from manga_autopilot.services.workflow_registry import (
    WorkflowAlreadyExistsError,
    WorkflowNotFoundError,
    WorkflowRegistry,
)

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/workflows"

DEFAULT_COMFY_BASE_URL = "http://127.0.0.1:8188"
DEFAULT_TEST_RUN_TIMEOUT_SEC = 300.0
DEFAULT_POLL_INTERVAL_SEC = 1.0
DEFAULT_MAX_POLL_ATTEMPTS = 600


def _serialise(workflow_id: str, wf) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "name": wf.name,
        "type": wf.type_value(),
        "file": wf.file,
        "bindings": {k: {"node_id": v.node_id, "input": v.input} for k, v in wf.bindings.items()},
        "description": wf.description,
    }


def _registry(app: Application) -> WorkflowRegistry:
    reg = app.get("manga_workflow_registry")
    if not isinstance(reg, WorkflowRegistry):
        raise web.HTTPInternalServerError(text="workflow registry is not initialised")
    return reg


def _comfy_client(app: Application) -> ComfyClient:
    existing = app.get("manga_comfy_client")
    if existing is not None and hasattr(existing, "submit_workflow"):
        return existing  # type: ignore[return-value]
    base_url = app.get("manga_comfy_base_url") or DEFAULT_COMFY_BASE_URL
    timeout = app.get("manga_comfy_test_run_timeout") or DEFAULT_TEST_RUN_TIMEOUT_SEC
    client = ComfyClient(base_url=base_url, timeout_sec=int(timeout))
    app["manga_comfy_client"] = client
    return client


async def _payload_or_400_async(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


def _apply_overrides(
    graph: dict[str, Any],
    bindings: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Mutate ``graph`` in place to apply the binding-driven overrides."""

    positive = overrides.get("positive_prompt", overrides.get("prompt"))
    negative = overrides.get("negative_prompt")
    seed = overrides.get("seed")
    width = overrides.get("width")
    height = overrides.get("height")
    steps = overrides.get("steps")
    cfg = overrides.get("cfg")
    reference_image = overrides.get("reference_image") or overrides.get("reference_image_path")
    reference_strength = overrides.get("reference_strength")
    lora_name = overrides.get("lora_name")
    lora_strength_model = overrides.get("lora_strength_model")
    lora_strength_clip = overrides.get("lora_strength_clip")
    ip_adapter_image = overrides.get("ip_adapter_image")
    ip_adapter_strength = overrides.get("ip_adapter_strength")
    filename_prefix = overrides.get("filename_prefix") or overrides.get("output_node")
    checkpoint = overrides.get("checkpoint")

    def _set_node_input(key: str, value: Any) -> None:
        binding = bindings.get(key)
        if not binding or not isinstance(binding, dict):
            return
        node_id = binding.get("node_id")
        input_name = binding.get("input")
        if not node_id or not input_name:
            return
        node = graph.get(node_id)
        if not isinstance(node, dict):
            return
        inputs = node.setdefault("inputs", {})
        if not isinstance(inputs, dict):
            return
        inputs[input_name] = value

    if positive is not None:
        _set_node_input("positive_prompt", positive)
    if negative is not None:
        _set_node_input("negative_prompt", negative)
    if seed is not None:
        _set_node_input("seed", int(seed))
    if width is not None:
        _set_node_input("width", int(width))
    if height is not None:
        _set_node_input("height", int(height))
    if steps is not None:
        _set_node_input("steps", int(steps))
    if cfg is not None:
        _set_node_input("cfg", float(cfg))
    if reference_image is not None:
        _set_node_input("reference_image", str(reference_image))
    if reference_strength is not None:
        _set_node_input("reference_strength", float(reference_strength))
    if lora_name is not None:
        _set_node_input("lora_name", str(lora_name))
    if lora_strength_model is not None:
        _set_node_input("lora_strength_model", float(lora_strength_model))
    if lora_strength_clip is not None:
        _set_node_input("lora_strength_clip", float(lora_strength_clip))
    if ip_adapter_image is not None:
        _set_node_input("ip_adapter_image", str(ip_adapter_image))
    if ip_adapter_strength is not None:
        _set_node_input("ip_adapter_strength", float(ip_adapter_strength))
    if filename_prefix is not None:
        _set_node_input("filename_prefix", str(filename_prefix))
    if checkpoint is not None:
        _set_node_input("checkpoint", str(checkpoint))
    return graph


# ----------------------------------------------------------------- GET list
async def list_workflows(request: web.Request) -> web.Response:
    reg = _registry(request.app)
    return web.json_response([_serialise(w.workflow_id, w) for w in reg.list()])


# ----------------------------------------------------------------- POST reg
async def create_workflow(request: web.Request) -> web.Response:
    body = await _payload_or_400_async(request)
    reg = _registry(request.app)
    try:
        wf = reg.register(body)
    except WorkflowValidationError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    except WorkflowAlreadyExistsError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    return web.json_response(_serialise(wf.workflow_id, wf), status=201)


# ----------------------------------------------------------------- GET one
async def get_workflow(request: web.Request) -> web.Response:
    wid = request.match_info["workflow_id"]
    reg = _registry(request.app)
    try:
        wf = reg.get(wid)
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(_serialise(wid, wf))


# ----------------------------------------------------------------- PATCH
async def update_workflow(request: web.Request) -> web.Response:
    wid = request.match_info["workflow_id"]
    body = await _payload_or_400_async(request)
    reg = _registry(request.app)
    try:
        wf = reg.update(wid, body)
    except WorkflowValidationError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(_serialise(wid, wf))


# ----------------------------------------------------------------- DELETE
async def delete_workflow(request: web.Request) -> web.Response:
    wid = request.match_info["workflow_id"]
    reg = _registry(request.app)
    try:
        reg.delete(wid)
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.Response(status=204)


# ------------------------------------------------------ POST /validate
async def validate_workflow(request: web.Request) -> web.Response:
    wid = request.match_info["workflow_id"]
    reg = _registry(request.app)
    try:
        wf = reg.get(wid)
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc

    graph_raw = wf.api_graph
    if graph_raw is None:
        raw_path = reg._workflow_path(wid)  # type: ignore[attr-defined]
        if raw_path.exists():
            import json as _json

            try:
                raw_payload = _json.loads(raw_path.read_text("utf-8"))
                graph_raw = raw_payload.get("api_graph")
            except _json.JSONDecodeError:
                graph_raw = None
    try:
        cleaned = validate_api_graph(graph_raw) if graph_raw is not None else {}
    except WorkflowValidationError as exc:
        return web.json_response(
            {"ok": False, "errors": [str(exc)]},
            status=200,
        )
    return web.json_response(
        {
            "ok": True,
            "workflow_id": wid,
            "nodes": list(cleaned),
            "required_bindings": list(wf.required_bindings()),
            "missing_bindings": sorted(set(wf.required_bindings()) - set(wf.bindings)),
        }
    )


# ------------------------------------------------------ POST /test-run
async def test_run_workflow(request: web.Request) -> web.Response:
    """Submit a workflow to ComfyUI, poll for completion, save /view images."""

    wid = request.match_info["workflow_id"]
    reg = _registry(request.app)
    try:
        wf = reg.get(wid)
    except WorkflowNotFoundError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc

    payload = await _payload_or_400_async(request)
    overrides = dict(payload.get("overrides") or {})
    output_dir = payload.get("output_dir")
    save_to = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else _default_test_run_dir(request.app, wid)
    )
    save_to.mkdir(parents=True, exist_ok=True)
    graph = wf.api_graph or {}
    try:
        cleaned = validate_api_graph(graph)
    except WorkflowValidationError as exc:
        raise web.HTTPBadRequest(text=f"workflow api_graph invalid: {exc}") from exc

    if not cleaned:
        raise web.HTTPBadRequest(
            text=(
                f"workflow {wid!r} has no api_graph; cannot test-run. "
                "Re-register the workflow with an embedded api_graph."
            )
        )

    bindings = {k: {"node_id": v.node_id, "input": v.input} for k, v in wf.bindings.items()}
    graph = _apply_overrides(cleaned, bindings, overrides)

    client = _comfy_client(request.app)
    try:
        prompt_id = await client.submit_workflow(graph)
    except ComfyUIRequestError as exc:
        return web.json_response(
            {
                "ok": False,
                "workflow_id": wid,
                "error": str(exc),
                "status": exc.status,
                "body": exc.body,
            },
            status=502,
        )
    except ComfyUIError as exc:
        return web.json_response(
            {"ok": False, "workflow_id": wid, "error": str(exc)},
            status=502,
        )

    try:
        images_meta, history_entry = await _wait_for_images(client, prompt_id)
    except (ComfyUIError, asyncio.TimeoutError) as exc:
        return web.json_response(
            {
                "ok": False,
                "workflow_id": wid,
                "prompt_id": prompt_id,
                "error": f"history poll failed: {exc}",
            },
            status=504,
        )

    saved: list[str] = []
    for idx, img in enumerate(images_meta):
        try:
            dest = save_to / f"{prompt_id}_{idx:03d}_{Path(img['filename']).name}"
            await client.fetch_image_to(
                dest,
                filename=img["filename"],
                subfolder=img.get("subfolder", ""),
                type=img.get("type", "output"),
            )
            saved.append(str(dest))
        except ComfyUIError as exc:
            log.warning("failed to fetch image %s: %s", img, exc)

    return web.json_response(
        {
            "ok": True,
            "workflow_id": wid,
            "prompt_id": prompt_id,
            "images_saved": saved,
            "history_status": history_entry.get("status") if isinstance(history_entry, dict) else None,
        }
    )


def _default_test_run_dir(app: Application, workflow_id: str) -> Path:
    """Pick a writable output dir for test-run artefacts."""

    storage_root = app.get("manga_storage_root")
    if storage_root is None:
        storage_root = Path.cwd()
    base = Path(storage_root) / "test_runs" / workflow_id
    return base


async def _wait_for_images(
    client: ComfyClient,
    prompt_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Poll ComfyUI ``/history/{prompt_id}`` until the prompt completes."""

    for _ in range(DEFAULT_MAX_POLL_ATTEMPTS):
        history = await client.get_history(prompt_id)
        entry = history.get(prompt_id) if isinstance(history, dict) else None
        if isinstance(entry, dict):
            status = entry.get("status")
            if isinstance(status, dict) and status.get("completed"):
                images = ComfyClient.extract_output_images(entry)
                if images:
                    return images, entry
            elif entry.get("outputs") and any(
                isinstance(v, dict) and v.get("images") for v in entry["outputs"].values()
            ):
                images = ComfyClient.extract_output_images(entry)
                if images:
                    return images, entry
        await asyncio.sleep(DEFAULT_POLL_INTERVAL_SEC)
    raise asyncio.TimeoutError(f"prompt {prompt_id} did not complete in time")


def register(router) -> None:  # type: ignore[no-untyped-def]
    """Register workflow routes on a ComfyUI router or :class:`Application`."""

    target = router.router if hasattr(router, "router") else router
    target.add_get(ROUTE_PREFIX, list_workflows)
    target.add_post(ROUTE_PREFIX, create_workflow)
    target.add_get(f"{ROUTE_PREFIX}/{{workflow_id}}", get_workflow)
    target.add_patch(f"{ROUTE_PREFIX}/{{workflow_id}}", update_workflow)
    target.add_delete(f"{ROUTE_PREFIX}/{{workflow_id}}", delete_workflow)
    target.add_post(f"{ROUTE_PREFIX}/{{workflow_id}}/validate", validate_workflow)
    target.add_post(f"{ROUTE_PREFIX}/{{workflow_id}}/test-run", test_run_workflow)
    log.debug("registered workflow routes under %s", ROUTE_PREFIX)


def register_routes(app: Application, *, prefix: str = ROUTE_PREFIX) -> None:
    app.router.add_get("", list_workflows)
    app.router.add_post("", create_workflow)
    app.router.add_get("/{workflow_id}", get_workflow)
    app.router.add_patch("/{workflow_id}", update_workflow)
    app.router.add_delete("/{workflow_id}", delete_workflow)
    app.router.add_post("/{workflow_id}/validate", validate_workflow)
    app.router.add_post("/{workflow_id}/test-run", test_run_workflow)
    log.debug("registered workflow routes under %s", prefix)


__all__ = [
    "ROUTE_PREFIX",
    "register_routes",
    "register",
    "list_workflows",
    "create_workflow",
    "get_workflow",
    "update_workflow",
    "delete_workflow",
    "validate_workflow",
    "test_run_workflow",
    "DEFAULT_COMFY_BASE_URL",
]
