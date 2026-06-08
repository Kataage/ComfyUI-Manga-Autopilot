"""Tests for the Modal worker MVP example.

Verifies that the pure-Python helpers in ``modal_worker.py`` work
correctly without the Modal SDK installed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODAL_WORKER_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "examples"
    / "modal-worker"
    / "modal_worker.py"
)


def _load_modal_worker():
    """Load modal_worker.py as a module regardless of package layout."""
    spec = importlib.util.spec_from_file_location("modal_worker", _MODAL_WORKER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["modal_worker"] = mod
    spec.loader.exec_module(mod)
    return mod


# Load once for all tests in this module.
mw = _load_modal_worker()


def test_modal_worker_example_imports_without_modal_sdk() -> None:
    """modal_worker.py can be imported even when modal is not installed."""
    assert hasattr(mw, "validate_generate_panel_payload")
    assert hasattr(mw, "build_success_response")
    assert hasattr(mw, "build_error_response")
    assert hasattr(mw, "make_placeholder_png_base64")
    # Pure-Python helpers should always be available.
    assert callable(mw.validate_generate_panel_payload)


def test_modal_worker_validates_required_payload_fields() -> None:
    """Missing required fields raise ValueError."""
    with pytest.raises(ValueError, match="missing required fields"):
        mw.validate_generate_panel_payload({})

    with pytest.raises(ValueError, match="missing required fields"):
        mw.validate_generate_panel_payload({"project_id": "x"})

    with pytest.raises(ValueError, match="missing required fields"):
        mw.validate_generate_panel_payload({
            "project_id": "x",
            "panel_id": "p",
            "prompt": "test",
            "width": 64,
            "height": 64,
        })


def test_modal_worker_validates_field_types() -> None:
    """Invalid field types raise ValueError."""
    with pytest.raises(ValueError, match="seed.*integer"):
        mw.validate_generate_panel_payload({
            "project_id": "x",
            "panel_id": "p",
            "prompt": "test",
            "seed": "not-int",
            "width": 64,
            "height": 64,
        })

    with pytest.raises(ValueError, match="width.*positive"):
        mw.validate_generate_panel_payload({
            "project_id": "x",
            "panel_id": "p",
            "prompt": "test",
            "seed": 42,
            "width": -1,
            "height": 64,
        })


def test_modal_worker_validates_complete_payload() -> None:
    """Complete valid payload passes validation."""
    payload = {
        "project_id": "proj_001",
        "panel_id": "panel_001_c00",
        "prompt": "1girl, masterpiece",
        "seed": 42,
        "width": 64,
        "height": 64,
    }
    result = mw.validate_generate_panel_payload(payload)
    assert result == payload


def test_modal_worker_builds_success_response_contract() -> None:
    """Success response matches RemoteHTTPExecutor contract."""
    payload = {
        "project_id": "proj_001",
        "panel_id": "panel_001_c00",
        "prompt": "1girl",
        "seed": 42,
        "width": 64,
        "height": 64,
    }
    resp = mw.build_success_response(payload, "aW1hZ2VfZGF0YQ==")

    assert resp["status"] == "completed"
    assert resp["image_base64"] == "aW1hZ2VfZGF0YQ=="
    assert resp["filename"] == "panel_001_c00.png"
    assert resp["seed"] == 42
    assert resp["metadata"]["executor"] == "modal-worker-mvp"


def test_modal_worker_builds_error_response_contract() -> None:
    """Error response matches RemoteHTTPExecutor contract."""
    resp = mw.build_error_response("model not configured")

    assert resp["status"] == "error"
    assert resp["error"] == "model not configured"


def test_modal_worker_placeholder_image_is_valid_base64() -> None:
    """Placeholder image produces valid base64 that decodes to bytes."""
    import base64

    b64 = mw.make_placeholder_png_base64(64, 64, 42)
    raw = base64.b64decode(b64)
    assert len(raw) > 0
    assert raw[:4] == b"\x89PNG"
