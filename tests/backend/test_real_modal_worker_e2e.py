"""Real Modal GPU Worker smoke test — opt-in E2E.

This test sends a real HTTP request to a deployed Modal worker.
It is skipped by default and only runs when both environment variables
are set:

    MANGA_AUTOPILOT_REAL_MODAL_E2E=1
    MANGA_AUTOPILOT_MODAL_WORKER_URL=https://your-modal-app.modal.run

Usage::

    MANGA_AUTOPILOT_REAL_MODAL_E2E=1 \\
    MANGA_AUTOPILOT_MODAL_WORKER_URL=https://your-modal-app.modal.run \\
    pytest tests/backend/test_real_modal_worker_e2e.py -q
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

_REAL_MODAL_E2E = os.environ.get("MANGA_AUTOPILOT_REAL_MODAL_E2E", "0") == "1"
_MODAL_WORKER_URL = os.environ.get("MANGA_AUTOPILOT_MODAL_WORKER_URL", "")

_SKIP_REASON = (
    "Real Modal E2E skipped.  Set MANGA_AUTOPILOT_REAL_MODAL_E2E=1 "
    "and MANGA_AUTOPILOT_MODAL_WORKER_URL to run."
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
    """POST to the Modal worker generate-panel endpoint."""
    import aiohttp

    url = f"{_MODAL_WORKER_URL.rstrip('/')}/v1/generate-panel"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            return await resp.json()


async def _get_job_status(job_id: str) -> dict[str, object]:
    """GET the Modal worker job status endpoint."""
    import aiohttp

    url = f"{_MODAL_WORKER_URL.rstrip('/')}/v1/jobs/{job_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            return await resp.json()


# --------------------------------------------------------------- smoke test


@pytest.mark.skipif(not _REAL_MODAL_E2E, reason=_SKIP_REASON)
@pytest.mark.skipif(not _MODAL_WORKER_URL, reason=_SKIP_REASON)
class TestRealModalWorkerE2E:
    """Real Modal worker smoke tests — requires deployed worker."""

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
        assert "executor" in metadata

    async def test_panel_id_in_filename(self):
        payload = _sample_payload(panel_id="panel_003_c01")
        resp = await _post_generate_panel(payload)

        assert "panel_003_c01" in resp.get("filename", "")
