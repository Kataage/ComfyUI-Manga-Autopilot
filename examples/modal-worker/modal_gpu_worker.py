"""Real Modal GPU Worker — opt-in MVP for Manga Autopilot.

This module provides a real Modal GPU worker that exposes HTTP endpoints
following the RemoteHTTPExecutor contract.  It requires the Modal SDK
(``pip install -e ".[modal]"``) and a Modal account.

CI does **not** run this module.  It is only activated when deployed
to Modal or run locally with ``modal serve``.

Usage::

    # Install Modal optional dependency
    pip install -e ".[modal]"

    # Run locally with Modal (requires Modal account)
    modal serve examples/modal-worker/modal_gpu_worker.py

    # Deploy to Modal
    modal deploy examples/modal-worker/modal_gpu_worker.py

The worker returns placeholder images by default.  Replace the
``_generate_image`` stub with real ComfyUI / model execution in a
follow-up issue.
"""

from __future__ import annotations

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
build_success_response = modal_worker.build_success_response
make_placeholder_png_base64 = modal_worker.make_placeholder_png_base64
validate_generate_panel_payload = modal_worker.validate_generate_panel_payload

try:
    import modal

    _HAS_MODAL = True
except ImportError:
    modal = None  # type: ignore[assignment]
    _HAS_MODAL = False

# --------------------------------------------------------------------------- config

GPU_TYPE = os.environ.get("MANGA_MODAL_GPU", "T4")
WORKER_TIMEOUT = int(os.environ.get("MANGA_MODAL_TIMEOUT", "300"))


# ----------------------------------------------------------------------- Modal app

if _HAS_MODAL:
    app = modal.App("manga-autopilot-gpu-worker")
    image = modal.Image.debian_slim().pip_install("pillow")

    # In-memory job store (per-container, resets on cold start).
    _jobs: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------- image gen

    def _generate_image(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate an image from a validated payload.

        MVP: returns a deterministic placeholder PNG.  Replace this
        with real ComfyUI workflow execution in a follow-up issue.
        """
        seed = payload["seed"]
        width = payload["width"]
        height = payload["height"]
        return {
            "image_base64": make_placeholder_png_base64(width, height, seed),
            "filename": f"{payload.get('panel_id', 'panel')}.png",
            "seed": seed,
        }

    # ----------------------------------------------------------------- HTTP app

    web_app = modal.web_app()

    @web_app.post("/v1/generate-panel")
    async def generate_panel(request: Any) -> dict[str, Any]:
        """POST /v1/generate-panel — synchronous panel generation.

        Follows the RemoteHTTPExecutor contract.  Returns a completed
        response with ``image_base64`` or an error.
        """
        try:
            body = await request.json()
        except Exception:
            return build_error_response("invalid JSON body")

        try:
            validated = validate_generate_panel_payload(body)
        except ValueError as exc:
            return build_error_response(str(exc))

        result = _generate_image(validated)
        return build_success_response(
            validated,
            result["image_base64"],
            filename=result["filename"],
            seed=result["seed"],
            metadata={
                "executor": "modal-gpu-worker-mvp",
                "gpu": GPU_TYPE,
            },
        )

    @web_app.post("/v1/generate-panel-async")
    async def generate_panel_async(request: Any) -> dict[str, Any]:
        """POST /v1/generate-panel-async — async panel generation.

        Returns a job_id immediately; poll GET /v1/jobs/{job_id}.
        """
        try:
            body = await request.json()
        except Exception:
            return build_error_response("invalid JSON body")

        try:
            validated = validate_generate_panel_payload(body)
        except ValueError as exc:
            return build_error_response(str(exc))

        import uuid

        job_id = f"job_{uuid.uuid4().hex[:12]}"
        _jobs[job_id] = {
            "status": "running",
            "job_id": job_id,
            "payload": validated,
            "created_at": time.time(),
        }

        # Simulate generation (real ComfyUI in follow-up issue).
        result = _generate_image(validated)
        _jobs[job_id].update(
            {
                "status": "completed",
                "filename": result["filename"],
                "image_base64": result["image_base64"],
                "seed": result["seed"],
                "metadata": {
                    "executor": "modal-gpu-worker-mvp",
                    "gpu": GPU_TYPE,
                },
            }
        )

        return {
            "status": "queued",
            "job_id": job_id,
            "metadata": {"executor": "modal-gpu-worker-mvp"},
        }

    @web_app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        """GET /v1/jobs/{job_id} — poll job status."""
        job = _jobs.get(job_id)
        if job is None:
            return build_error_response(f"job {job_id} not found")
        return {k: v for k, v in job.items() if k != "payload"}

    @web_app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """POST /v1/jobs/{job_id}/cancel — cancel a running job."""
        job = _jobs.get(job_id)
        if job is None:
            return build_error_response(f"job {job_id} not found")
        if job["status"] in ("completed", "error"):
            return {
                "status": job["status"],
                "job_id": job_id,
            }
        job["status"] = "cancelled"
        return {
            "status": "cancelled",
            "job_id": job_id,
        }
