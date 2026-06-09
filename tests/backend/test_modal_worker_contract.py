"""Modal worker contract tests — run in standard CI without Modal SDK.

These tests verify that the Modal worker helpers follow the
RemoteHTTPExecutor contract.  No Modal SDK or GPU is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure examples/modal-worker is importable.
_EXAMPLES_DIR = str(Path(__file__).resolve().parents[2] / "examples" / "modal-worker")
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from modal_worker import (  # noqa: E402
    REQUIRED_FIELDS,
    build_error_response,
    build_success_response,
    make_placeholder_png_base64,
    validate_generate_panel_payload,
)

# ----------------------------------------------------------------- sample payload

def _sample_payload(**overrides: object) -> dict[str, object]:
    base = {
        "project_id": "test-project",
        "page_id": "page_0001",
        "panel_id": "panel_001_c00",
        "prompt": "1girl, masterpiece",
        "negative_prompt": "lowres, blurry",
        "seed": 42,
        "width": 512,
        "height": 768,
        "workflow_id": "anime_t2i_default",
        "metadata": {"run_id": "run_20260609_123456_aabbccdd"},
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------- payload validation


class TestPayloadValidation:
    """validate_generate_panel_payload contract tests."""

    def test_valid_payload_passes(self):
        payload = _sample_payload()
        result = validate_generate_panel_payload(payload)
        assert result == payload

    def test_missing_project_id_raises(self):
        payload = _sample_payload()
        del payload["project_id"]
        try:
            validate_generate_panel_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "project_id" in str(exc)

    def test_missing_panel_id_raises(self):
        payload = _sample_payload()
        del payload["panel_id"]
        try:
            validate_generate_panel_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "panel_id" in str(exc)

    def test_missing_seed_raises(self):
        payload = _sample_payload()
        del payload["seed"]
        try:
            validate_generate_panel_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "seed" in str(exc)

    def test_non_dict_payload_raises(self):
        try:
            validate_generate_panel_payload("not a dict")
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "JSON object" in str(exc)

    def test_non_int_seed_raises(self):
        payload = _sample_payload(seed="42")
        try:
            validate_generate_panel_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "seed" in str(exc)

    def test_non_positive_width_raises(self):
        payload = _sample_payload(width=0)
        try:
            validate_generate_panel_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "width" in str(exc)

    def test_non_positive_height_raises(self):
        payload = _sample_payload(height=-1)
        try:
            validate_generate_panel_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "height" in str(exc)

    def test_all_required_fields_listed(self):
        assert "project_id" in REQUIRED_FIELDS
        assert "panel_id" in REQUIRED_FIELDS
        assert "prompt" in REQUIRED_FIELDS
        assert "seed" in REQUIRED_FIELDS
        assert "width" in REQUIRED_FIELDS
        assert "height" in REQUIRED_FIELDS


# ---------------------------------------------------- success response contract


class TestSuccessResponseContract:
    """build_success_response follows RemoteHTTPExecutor response format."""

    def test_has_status_completed(self):
        payload = _sample_payload()
        resp = build_success_response(payload, "aW1hZ2U=")
        assert resp["status"] == "completed"

    def test_has_image_base64(self):
        payload = _sample_payload()
        resp = build_success_response(payload, "aW1hZ2U=")
        assert resp["image_base64"] == "aW1hZ2U="

    def test_has_filename(self):
        payload = _sample_payload(panel_id="panel_001_c00")
        resp = build_success_response(payload, "aW1hZ2U=")
        assert resp["filename"] == "panel_001_c00.png"

    def test_custom_filename(self):
        payload = _sample_payload()
        resp = build_success_response(payload, "aW1hZ2U=", filename="custom.png")
        assert resp["filename"] == "custom.png"

    def test_has_seed(self):
        payload = _sample_payload(seed=99)
        resp = build_success_response(payload, "aW1hZ2U=", seed=99)
        assert resp["seed"] == 99

    def test_fallback_seed_from_payload(self):
        payload = _sample_payload(seed=77)
        resp = build_success_response(payload, "aW1hZ2U=")
        assert resp["seed"] == 77

    def test_has_metadata_executor(self):
        payload = _sample_payload()
        resp = build_success_response(payload, "aW1hZ2U=")
        assert "metadata" in resp
        assert resp["metadata"]["executor"] == "modal-worker-mvp"

    def test_metadata_merges_custom(self):
        payload = _sample_payload()
        resp = build_success_response(
            payload, "aW1hZ2U=", metadata={"custom_key": "value"}
        )
        assert resp["metadata"]["custom_key"] == "value"
        assert resp["metadata"]["executor"] == "modal-worker-mvp"


# ---------------------------------------------------- error response contract


class TestErrorResponseContract:
    """build_error_response follows RemoteHTTPExecutor error format."""

    def test_has_status_error(self):
        resp = build_error_response("something failed")
        assert resp["status"] == "error"

    def test_has_error_message(self):
        resp = build_error_response("model not found")
        assert resp["error"] == "model not found"

    def test_no_image_field(self):
        resp = build_error_response("fail")
        assert "image_base64" not in resp
        assert "artifact_url" not in resp


# -------------------------------------------------- placeholder image generation


class TestPlaceholderImage:
    """make_placeholder_png_base64 produces valid base64-encoded PNG."""

    def test_returns_base64_string(self):
        b64 = make_placeholder_png_base64(64, 64, 42)
        assert isinstance(b64, str)
        assert len(b64) > 0

    def test_deterministic_same_seed(self):
        a = make_placeholder_png_base64(64, 64, 42)
        b = make_placeholder_png_base64(64, 64, 42)
        assert a == b

    def test_different_seeds_differ(self):
        a = make_placeholder_png_base64(64, 64, 1)
        b = make_placeholder_png_base64(64, 64, 2)
        assert a != b

    def test_different_dimensions_differ(self):
        a = make_placeholder_png_base64(64, 64, 42)
        b = make_placeholder_png_base64(128, 128, 42)
        assert a != b

    def test_base64_decodes_to_png(self):
        import base64

        b64 = make_placeholder_png_base64(32, 32, 1)
        raw = base64.b64decode(b64)
        assert raw[:4] == b"\x89PNG"


# ----------------------------------------------- Modal SDK import (skip if missing)


class TestModalSDKImport:
    """modal_gpu_worker module can be imported (with or without Modal SDK)."""

    def test_import_without_modal(self):
        """Importing modal_worker module should not require Modal SDK."""
        import importlib

        mod = importlib.import_module("modal_worker")
        assert hasattr(mod, "validate_generate_panel_payload")
        assert hasattr(mod, "build_success_response")
        assert hasattr(mod, "build_error_response")

    def test_has_modal_flag(self):
        import modal_worker

        assert hasattr(modal_worker, "_HAS_MODAL")
        assert isinstance(modal_worker._HAS_MODAL, bool)
