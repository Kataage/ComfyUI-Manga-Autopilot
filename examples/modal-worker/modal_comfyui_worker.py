"""Modal ComfyUI execution worker — opt-in MVP for Manga Autopilot.

This module provides a Modal GPU worker that executes ComfyUI API
workflows.  It requires the Modal SDK (``pip install -e ".[modal]"``)
and a Modal account with a pre-populated Volume containing checkpoints.

CI does **not** run this module.  It is only activated when deployed
to Modal or run locally with ``modal serve``.

Usage::

    # Install Modal optional dependency
    pip install -e ".[modal]"

    # Authenticate with Modal
    modal setup

    # Create volume and add checkpoints
    modal volume create manga-autopilot-comfyui

    # Deploy to Modal
    modal deploy examples/modal-worker/modal_comfyui_worker.py

Checkpoints must be placed on the Modal Volume by the user.
No automatic model downloads are performed.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Add parent directory to path so we can import modal_worker helpers.
_PARENT = str(Path(__file__).resolve().parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import modal_worker  # noqa: E402

build_error_response = modal_worker.build_error_response
validate_generate_panel_payload = modal_worker.validate_generate_panel_payload

try:
    import modal

    _HAS_MODAL = True
except ImportError:
    modal = None  # type: ignore[assignment]
    _HAS_MODAL = False

# --------------------------------------------------------------------------- config

COMFYUI_PORT = int(os.environ.get("MANGA_MODAL_COMFYUI_PORT", "8188"))
COMFYUI_HOST = os.environ.get("MANGA_MODAL_COMFYUI_HOST", "127.0.0.1")
COMFYUI_ROOT = os.environ.get("MANGA_MODAL_COMFYUI_ROOT", "/root/ComfyUI")
COMFYUI_STARTUP_TIMEOUT = int(os.environ.get("MANGA_MODAL_COMFYUI_STARTUP_TIMEOUT", "120"))
COMFYUI_REQUEST_TIMEOUT = int(os.environ.get("MANGA_MODAL_COMFYUI_REQUEST_TIMEOUT", "300"))
COMFYUI_POLL_INTERVAL = float(os.environ.get("MANGA_MODAL_COMFYUI_POLL_INTERVAL", "1.0"))
VOLUME_NAME = os.environ.get("MANGA_AUTOPILOT_MODAL_VOLUME_NAME", "manga-autopilot-comfyui")
OUTPUT_DIR = os.environ.get("MANGA_MODAL_OUTPUT_DIR", "/outputs")

# ----------------------------------------------------- workflow registry helpers


def load_workflow_registry(path: str | Path) -> dict[str, Any]:
    """Load a workflow registry JSON file.

    Returns the parsed registry dict.  Raises ``FileNotFoundError``
    if the file does not exist, or ``json.JSONDecodeError`` if invalid.
    """
    with open(path) as f:
        return json.load(f)


def inject_bindings(
    api_graph: dict[str, Any],
    bindings: dict[str, Any],
    values: dict[str, Any],
) -> dict[str, Any]:
    """Inject binding values into an API graph.

    For each binding, looks up the target ``node_id`` and ``input`` in
    the ``api_graph`` and sets the value from ``values``.

    Returns a new graph (does not mutate the original).
    """
    graph = copy.deepcopy(api_graph)
    for key, binding in bindings.items():
        if key not in values:
            continue
        node_id = str(binding["node_id"])
        input_name = binding["input"]
        if node_id in graph and "inputs" in graph[node_id]:
            graph[node_id]["inputs"][input_name] = values[key]
    return graph


def resolve_checkpoint_path(
    checkpoint_name: str,
    comfyui_root: str | Path | None = None,
) -> str:
    """Resolve the full path to a checkpoint file.

    Checks standard ComfyUI checkpoint locations.  Raises
    ``FileNotFoundError`` if the checkpoint is not found.
    """
    root = Path(comfyui_root or COMFYUI_ROOT)
    candidates = [
        root / "models" / "checkpoints" / checkpoint_name,
        Path(checkpoint_name),  # Absolute path case.
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(f"checkpoint not found: {checkpoint_name}")


# -------------------------------------------------------- ComfyUI HTTP client


async def _comfyui_get(
    url: str,
    timeout: float = COMFYUI_REQUEST_TIMEOUT,
) -> dict[str, Any] | bytes:
    """HTTP GET from local ComfyUI server."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return await resp.json()
            return await resp.read()


async def _comfyui_post(
    url: str,
    payload: dict[str, Any],
    timeout: float = COMFYUI_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    """HTTP POST to local ComfyUI server."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            return await resp.json()


async def wait_for_comfyui(
    host: str = COMFYUI_HOST,
    port: int = COMFYUI_PORT,
    timeout: float = COMFYUI_STARTUP_TIMEOUT,
) -> None:
    """Wait until ComfyUI server is ready (``/object_info`` responds).

    Raises ``TimeoutError`` if the server does not become ready in time.
    """
    url = f"http://{host}:{port}/object_info"
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            result = await _comfyui_get(url, timeout=5)
            if isinstance(result, dict):
                return
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(2)
    msg = f"ComfyUI server not ready after {timeout}s"
    if last_error:
        msg += f": {last_error}"
    raise TimeoutError(msg)


async def submit_prompt(
    api_graph: dict[str, Any],
    host: str = COMFYUI_HOST,
    port: int = COMFYUI_PORT,
) -> str:
    """Submit a prompt to ComfyUI ``/prompt`` and return the prompt_id."""
    payload = {"prompt": api_graph}
    result = await _comfyui_post(
        f"http://{host}:{port}/prompt",
        payload,
    )
    if "prompt_id" not in result:
        raise RuntimeError(f"ComfyUI /prompt failed: {result}")
    return result["prompt_id"]


async def poll_history(
    prompt_id: str,
    host: str = COMFYUI_HOST,
    port: int = COMFYUI_PORT,
    timeout: float = COMFYUI_REQUEST_TIMEOUT,
    poll_interval: float = COMFYUI_POLL_INTERVAL,
) -> dict[str, Any]:
    """Poll ``/history/{prompt_id}`` until completion.

    Returns the history entry dict.  Raises ``TimeoutError`` if not
    completed within the timeout.
    """
    url = f"http://{host}:{port}/history/{prompt_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = await _comfyui_get(url, timeout=10)
        if isinstance(result, dict) and prompt_id in result:
            return result[prompt_id]
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"ComfyUI history not ready after {timeout}s")


def extract_output_image(
    history_entry: dict[str, Any],
) -> tuple[bytes, str]:
    """Extract the first output image from a ComfyUI history entry.

    Returns ``(image_bytes, filename)``.
    Raises ``RuntimeError`` if no output image is found.
    """
    outputs = history_entry.get("outputs", {})
    for _node_id, node_output in outputs.items():
        images = node_output.get("images", [])
        if images:
            img = images[0]
            return img["data"], img.get("filename", "output.png")
    raise RuntimeError("no output image found in ComfyUI history")


async def fetch_image_bytes(
    filename: str,
    subfolder: str = "",
    img_type: str = "output",
    host: str = COMFYUI_HOST,
    port: int = COMFYUI_PORT,
) -> bytes:
    """Fetch an image from ComfyUI ``/view`` endpoint."""
    params = f"filename={filename}&subfolder={subfolder}&type={img_type}"
    url = f"http://{host}:{port}/view?{params}"
    return await _comfyui_get(url, timeout=30)  # type: ignore[return-value]


# ----------------------------------------------------- response builders


def build_comfyui_success_response(
    payload: dict[str, Any],
    image_base64: str,
    *,
    seed: int | None = None,
    run_id: str | None = None,
    workflow_id: str | None = None,
    prompt_id: str | None = None,
) -> dict[str, Any]:
    """Build a successful ComfyUI execution response."""
    panel_id = payload.get("panel_id", "panel")
    metadata: dict[str, Any] = {"executor": "modal-comfyui"}
    if run_id:
        metadata["run_id"] = run_id
    if workflow_id:
        metadata["workflow_id"] = workflow_id
    if prompt_id:
        metadata["prompt_id"] = prompt_id
    return {
        "status": "completed",
        "filename": f"{panel_id}.png",
        "image_base64": image_base64,
        "seed": seed or payload.get("seed", 0),
        "metadata": metadata,
    }


def build_comfyui_error_response(
    message: str,
    *,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Build an error ComfyUI execution response."""
    metadata: dict[str, Any] = {"executor": "modal-comfyui"}
    if workflow_id:
        metadata["workflow_id"] = workflow_id
    return {
        "status": "error",
        "error": message,
        "metadata": metadata,
    }


# -------------------------------------------------- pure-Python validation


COMFYUI_REQUIRED_FIELDS = ("project_id", "panel_id", "prompt", "seed", "width", "height")


def validate_comfyui_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a ComfyUI execution payload.

    Same fields as RemoteHTTPExecutor plus optional ``workflow_id``.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    missing = [f for f in COMFYUI_REQUIRED_FIELDS if f not in payload]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    seed = payload.get("seed")
    if not isinstance(seed, int):
        raise ValueError("'seed' must be an integer")
    width = payload.get("width")
    height = payload.get("height")
    if not isinstance(width, int) or width <= 0:
        raise ValueError("'width' must be a positive integer")
    if not isinstance(height, int) or height <= 0:
        raise ValueError("'height' must be a positive integer")
    return payload


# ----------------------------------------------------------------------- Modal app

if _HAS_MODAL:
    app = modal.App("manga-autopilot-comfyui-worker")
    image = modal.Image.debian_slim().pip_install("pillow", "aiohttp")
    volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

    @app.function(
        image=image,
        volumes={"/comfyui-volume": volume},
        timeout=600,
        gpu="T4",
    )
    def execute_comfyui_workflow(payload: dict[str, Any]) -> dict[str, Any]:
        """Modal function entry point for ComfyUI workflow execution.

        Receives a validated /v1/generate-panel payload and executes
        a ComfyUI API workflow, returning the generated image.
        """

        async def _run() -> dict[str, Any]:
            try:
                validated = validate_comfyui_payload(payload)
            except ValueError as exc:
                return build_comfyui_error_response(str(exc))

            workflow_id = validated.get("workflow_id", "anime_t2i_default")
            run_id = validated.get("metadata", {}).get("run_id")

            # Resolve workflow registry.
            registry_path = (
                Path("/comfyui-volume") / "workflows" / f"{workflow_id}.registry.json"
            )
            if not registry_path.exists():
                # Fallback to examples/workflows/ in the repo.
                registry_path = (
                    Path(__file__).resolve().parents[2]
                    / "examples"
                    / "workflows"
                    / f"{workflow_id}.registry.json"
                )
            if not registry_path.exists():
                return build_comfyui_error_response(
                    f"workflow registry not found: {workflow_id}",
                    workflow_id=workflow_id,
                )

            try:
                registry = load_workflow_registry(registry_path)
            except Exception as exc:
                return build_comfyui_error_response(
                    f"failed to load workflow registry: {exc}",
                    workflow_id=workflow_id,
                )

            bindings = registry.get("bindings", {})
            api_graph = registry.get("api_graph", {})
            if not api_graph:
                return build_comfyui_error_response(
                    "workflow registry has no api_graph",
                    workflow_id=workflow_id,
                )

            # Inject values.
            values: dict[str, Any] = {
                "positive_prompt": validated["prompt"],
                "negative_prompt": validated.get("negative_prompt", ""),
                "seed": validated["seed"],
                "width": validated["width"],
                "height": validated["height"],
            }
            # Check if checkpoint binding exists and validate it.
            if "checkpoint" in bindings:
                ckpt_binding = bindings["checkpoint"]
                ckpt_node_id = str(ckpt_binding["node_id"])
                ckpt_name = api_graph.get(ckpt_node_id, {}).get("inputs", {}).get(
                    ckpt_binding["input"], ""
                )
                try:
                    resolve_checkpoint_path(ckpt_name)
                except FileNotFoundError as exc:
                    return build_comfyui_error_response(
                        str(exc), workflow_id=workflow_id
                    )

            graph = inject_bindings(api_graph, bindings, values)

            # Start ComfyUI subprocess.
            import subprocess

            comfyui_main = Path(COMFYUI_ROOT) / "main.py"
            if not comfyui_main.exists():
                return build_comfyui_error_response(
                    f"ComfyUI main.py not found at {comfyui_main}",
                    workflow_id=workflow_id,
                )

            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(comfyui_main),
                    "--listen", COMFYUI_HOST,
                    "--port", str(COMFYUI_PORT),
                    "--dont-print-server",
                ],
                cwd=str(COMFYUI_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            try:
                # Wait for server.
                await wait_for_comfyui()

                # Submit prompt.
                prompt_id = await submit_prompt(graph)

                # Poll for completion.
                history_entry = await poll_history(prompt_id)

                # Extract image.
                img_bytes, filename = extract_output_image(history_entry)
                img_b64 = base64.b64encode(img_bytes).decode("ascii")

                return build_comfyui_success_response(
                    validated,
                    img_b64,
                    seed=validated["seed"],
                    run_id=run_id,
                    workflow_id=workflow_id,
                    prompt_id=prompt_id,
                )
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        return asyncio.run(_run())
