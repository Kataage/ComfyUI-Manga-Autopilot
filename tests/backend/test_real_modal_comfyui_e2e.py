"""Real Modal ComfyUI worker smoke test — opt-in E2E.

This test sends a real HTTP request to a deployed Modal ComfyUI worker.
It is skipped by default and only runs when all three environment
variables are set:

    MANGA_AUTOPILOT_REAL_MODAL_COMFYUI_E2E=1
    MANGA_AUTOPILOT_MODAL_COMFYUI_WORKER_URL=https://your-modal-app.modal.run
    MANGA_AUTOPILOT_MODAL_COMFYUI_WORKFLOW_JSON=examples/workflows/anime_t2i_default.registry.json

Usage::

    MANGA_AUTOPILOT_REAL_MODAL_COMFYUI_E2E=1 \\
    MANGA_AUTOPILOT_MODAL_COMFYUI_WORKER_URL=https://your-modal-app.modal.run \\
    MANGA_AUTOPILOT_MODAL_COMFYUI_WORKFLOW_JSON=examples/workflows/anime_t2i_default.registry.json \\
    pytest tests/backend/test_real_modal_comfyui_e2e.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure examples/modal-worker is importable.
_EXAMPLES_DIR = str(Path(__file__).resolve().parents[2] / "examples" / "modal-worker")
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)


# ------------------------------------------------------------------ skip guard

_REAL_MODAL_COMFYUI_E2E = os.environ.get("MANGA_AUTOPILOT_REAL_MODAL_COMFYUI_E2E", "0") == "1"
_MODAL_COMFYUI_WORKER_URL = os.environ.get("MANGA_AUTOPILOT_MODAL_COMFYUI_WORKER_URL", "")
_MODAL_COMFYUI_WORKFLOW_JSON = os.environ.get("MANGA_AUTOPILOT_MODAL_COMFYUI_WORKFLOW_JSON", "")

_SKIP_REASON = (
    "Real Modal ComfyUI E2E skipped.  Set "
    "MANGA_AUTOPILOT_REAL_MODAL_COMFYUI_E2E=1, "
    "MANGA_AUTOPILOT_MODAL_COMFYUI_WORKER_URL, and "
    "MANGA_AUTOPILOT_MODAL_COMFYUI_WORKFLOW_JSON to run."
)


# ------------------------------------------------------------------- helpers


def _sample_payload() -> dict[str, object]:
    return {
        "project_id": "test-project",
        "page_id": "page_0001",
        "panel_id": "panel_001_c00",
        "prompt": "1girl, masterpiece, best quality",
        "negative_prompt": "lowres, blurry, bad anatomy",
        "seed": 42,
        "width": 512,
        "height": 768,
        "workflow_id": "anime_t2i_default",
        "metadata": {"run_id": "run_20260609_123456_aabbccdd"},
    }


async def _post_generate_panel(
    payload: dict[str, object],
) -> dict[str, object]:
    """POST to the Modal ComfyUI worker generate-panel endpoint."""
    import aiohttp

    url = f"{_MODAL_COMFYUI_WORKER_URL.rstrip('/')}/v1/generate-panel"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            return await resp.json()


# --------------------------------------------------------------- smoke test


@pytest.mark.skipif(not _REAL_MODAL_COMFYUI_E2E, reason=_SKIP_REASON)
@pytest.mark.skipif(not _MODAL_COMFYUI_WORKER_URL, reason=_SKIP_REASON)
@pytest.mark.skipif(not _MODAL_COMFYUI_WORKFLOW_JSON, reason=_SKIP_REASON)
class TestRealModalComfyuiE2E:
    """Real Modal ComfyUI worker smoke tests — requires deployed worker."""

    async def test_generate_panel_returns_completed(self):
        payload = _sample_payload()
        resp = await _post_generate_panel(payload)

        assert resp["status"] == "completed", f"expected completed, got {resp}"
        assert "image_base64" in resp or "artifact_url" in resp
        assert resp.get("seed") == payload["seed"]

    async def test_image_base64_decodes_to_png(self):
        import base64

        payload = _sample_payload()
        resp = await _post_generate_panel(payload)

        if "image_base64" in resp:
            raw = base64.b64decode(resp["image_base64"])
            assert raw[:4] == b"\x89PNG", "image_base64 does not decode to PNG"

    async def test_metadata_has_executor(self):
        payload = _sample_payload()
        resp = await _post_generate_panel(payload)

        metadata = resp.get("metadata", {})
        assert metadata.get("executor") == "modal-comfyui"

    async def test_metadata_has_workflow_id(self):
        payload = _sample_payload()
        resp = await _post_generate_panel(payload)

        metadata = resp.get("metadata", {})
        assert "workflow_id" in metadata

    async def test_image_is_pil_compatible(self):
        import base64
        from io import BytesIO

        from PIL import Image

        payload = _sample_payload()
        resp = await _post_generate_panel(payload)

        if "image_base64" in resp:
            raw = base64.b64decode(resp["image_base64"])
            img = Image.open(BytesIO(raw))
            assert img.size[0] > 0
            assert img.size[1] > 0
