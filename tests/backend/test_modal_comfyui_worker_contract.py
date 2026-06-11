"""Modal ComfyUI worker contract tests — standard CI, no Modal/ComfyUI required.

These tests verify that the ComfyUI worker helpers follow the
RemoteHTTPExecutor contract.  No Modal SDK or GPU is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure examples/modal-worker is importable.
_EXAMPLES_DIR = str(Path(__file__).resolve().parents[2] / "examples" / "modal-worker")
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from modal_comfyui_worker import (  # noqa: E402
    COMFYUI_REQUIRED_FIELDS,
    build_comfyui_error_response,
    build_comfyui_success_response,
    inject_bindings,
    load_workflow_registry,
    resolve_checkpoint_path,
    validate_comfyui_payload,
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


# ----------------------------------------------------------------- import test


class TestImportWithoutModalSDK:
    """modal_comfyui_worker module can be imported without Modal SDK."""

    def test_import_without_modal(self):
        import importlib

        mod = importlib.import_module("modal_comfyui_worker")
        assert hasattr(mod, "validate_comfyui_payload")
        assert hasattr(mod, "build_comfyui_success_response")
        assert hasattr(mod, "build_comfyui_error_response")
        assert hasattr(mod, "inject_bindings")
        assert hasattr(mod, "load_workflow_registry")

    def test_has_modal_flag(self):
        import modal_comfyui_worker

        assert hasattr(modal_comfyui_worker, "_HAS_MODAL")
        assert isinstance(modal_comfyui_worker._HAS_MODAL, bool)


# ----------------------------------------------------------- payload validation


class TestPayloadValidation:
    """validate_comfyui_payload contract tests."""

    def test_valid_payload_passes(self):
        payload = _sample_payload()
        result = validate_comfyui_payload(payload)
        assert result == payload

    def test_accepts_remote_executor_payload(self):
        """Same payload format as RemoteHTTPExecutor."""
        payload = _sample_payload()
        result = validate_comfyui_payload(payload)
        assert result["project_id"] == "test-project"
        assert result["panel_id"] == "panel_001_c00"

    def test_rejects_missing_prompt(self):
        payload = _sample_payload()
        del payload["prompt"]
        try:
            validate_comfyui_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "prompt" in str(exc)

    def test_rejects_missing_project_id(self):
        payload = _sample_payload()
        del payload["project_id"]
        try:
            validate_comfyui_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "project_id" in str(exc)

    def test_rejects_missing_panel_id(self):
        payload = _sample_payload()
        del payload["panel_id"]
        try:
            validate_comfyui_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "panel_id" in str(exc)

    def test_rejects_missing_seed(self):
        payload = _sample_payload()
        del payload["seed"]
        try:
            validate_comfyui_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "seed" in str(exc)

    def test_rejects_non_dict_payload(self):
        try:
            validate_comfyui_payload("not a dict")
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "JSON object" in str(exc)

    def test_rejects_non_int_seed(self):
        payload = _sample_payload(seed="42")
        try:
            validate_comfyui_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "seed" in str(exc)

    def test_rejects_zero_width(self):
        payload = _sample_payload(width=0)
        try:
            validate_comfyui_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "width" in str(exc)

    def test_rejects_negative_height(self):
        payload = _sample_payload(height=-1)
        try:
            validate_comfyui_payload(payload)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "height" in str(exc)

    def test_all_required_fields_listed(self):
        assert "project_id" in COMFYUI_REQUIRED_FIELDS
        assert "panel_id" in COMFYUI_REQUIRED_FIELDS
        assert "prompt" in COMFYUI_REQUIRED_FIELDS
        assert "seed" in COMFYUI_REQUIRED_FIELDS
        assert "width" in COMFYUI_REQUIRED_FIELDS
        assert "height" in COMFYUI_REQUIRED_FIELDS

    def test_optional_workflow_id_accepted(self):
        payload = _sample_payload(workflow_id="custom_workflow")
        result = validate_comfyui_payload(payload)
        assert result["workflow_id"] == "custom_workflow"

    def test_optional_negative_prompt_accepted(self):
        payload = _sample_payload(negative_prompt="ugly, blurry")
        result = validate_comfyui_payload(payload)
        assert result["negative_prompt"] == "ugly, blurry"


# ---------------------------------------------------- success response contract


class TestSuccessResponseContract:
    """build_comfyui_success_response follows RemoteHTTPExecutor format."""

    def test_has_status_completed(self):
        payload = _sample_payload()
        resp = build_comfyui_success_response(payload, "aW1hZ2U=")
        assert resp["status"] == "completed"

    def test_has_image_base64(self):
        payload = _sample_payload()
        resp = build_comfyui_success_response(payload, "aW1hZ2U=")
        assert resp["image_base64"] == "aW1hZ2U="

    def test_has_filename(self):
        payload = _sample_payload(panel_id="panel_001_c00")
        resp = build_comfyui_success_response(payload, "aW1hZ2U=")
        assert resp["filename"] == "panel_001_c00.png"

    def test_has_seed(self):
        payload = _sample_payload(seed=99)
        resp = build_comfyui_success_response(payload, "aW1hZ2U=", seed=99)
        assert resp["seed"] == 99

    def test_fallback_seed_from_payload(self):
        payload = _sample_payload(seed=77)
        resp = build_comfyui_success_response(payload, "aW1hZ2U=")
        assert resp["seed"] == 77

    def test_metadata_has_executor(self):
        payload = _sample_payload()
        resp = build_comfyui_success_response(payload, "aW1hZ2U=")
        assert resp["metadata"]["executor"] == "modal-comfyui"

    def test_metadata_has_workflow_id(self):
        payload = _sample_payload()
        resp = build_comfyui_success_response(
            payload, "aW1hZ2U=", workflow_id="anime_t2i_default"
        )
        assert resp["metadata"]["workflow_id"] == "anime_t2i_default"

    def test_metadata_has_prompt_id(self):
        payload = _sample_payload()
        resp = build_comfyui_success_response(
            payload, "aW1hZ2U=", prompt_id="abc-123"
        )
        assert resp["metadata"]["prompt_id"] == "abc-123"

    def test_metadata_has_run_id(self):
        payload = _sample_payload()
        resp = build_comfyui_success_response(
            payload, "aW1hZ2U=", run_id="run_20260609_123456"
        )
        assert resp["metadata"]["run_id"] == "run_20260609_123456"


# ---------------------------------------------------- error response contract


class TestErrorResponseContract:
    """build_comfyui_error_response follows RemoteHTTPExecutor format."""

    def test_has_status_error(self):
        resp = build_comfyui_error_response("something failed")
        assert resp["status"] == "error"

    def test_has_error_message(self):
        resp = build_comfyui_error_response("checkpoint not found")
        assert resp["error"] == "checkpoint not found"

    def test_has_metadata_executor(self):
        resp = build_comfyui_error_response("fail")
        assert resp["metadata"]["executor"] == "modal-comfyui"

    def test_has_metadata_workflow_id(self):
        resp = build_comfyui_error_response("fail", workflow_id="test")
        assert resp["metadata"]["workflow_id"] == "test"

    def test_no_image_field(self):
        resp = build_comfyui_error_response("fail")
        assert "image_base64" not in resp
        assert "artifact_url" not in resp


# -------------------------------------------------- workflow binding injection


class TestWorkflowBindingInjection:
    """inject_bindings correctly injects values into API graphs."""

    def test_injects_positive_prompt(self):
        api_graph = {
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "original", "clip": ["4", 1]},
            }
        }
        bindings = {"positive_prompt": {"node_id": "6", "input": "text"}}
        values = {"positive_prompt": "1girl, masterpiece"}
        result = inject_bindings(api_graph, bindings, values)
        assert result["6"]["inputs"]["text"] == "1girl, masterpiece"

    def test_injects_seed(self):
        api_graph = {"3": {"class_type": "KSampler", "inputs": {"seed": 0}}}
        bindings = {"seed": {"node_id": "3", "input": "seed"}}
        values = {"seed": 42}
        result = inject_bindings(api_graph, bindings, values)
        assert result["3"]["inputs"]["seed"] == 42

    def test_injects_width_and_height(self):
        api_graph = {
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 0, "height": 0},
            }
        }
        bindings = {
            "width": {"node_id": "5", "input": "width"},
            "height": {"node_id": "5", "input": "height"},
        }
        values = {"width": 1024, "height": 768}
        result = inject_bindings(api_graph, bindings, values)
        assert result["5"]["inputs"]["width"] == 1024
        assert result["5"]["inputs"]["height"] == 768

    def test_does_not_mutate_original(self):
        api_graph = {"3": {"class_type": "KSampler", "inputs": {"seed": 0}}}
        bindings = {"seed": {"node_id": "3", "input": "seed"}}
        values = {"seed": 99}
        inject_bindings(api_graph, bindings, values)
        assert api_graph["3"]["inputs"]["seed"] == 0

    def test_skips_missing_binding_key(self):
        api_graph = {"3": {"class_type": "KSampler", "inputs": {"seed": 0}}}
        bindings = {"seed": {"node_id": "3", "input": "seed"}}
        values = {}  # No seed value.
        result = inject_bindings(api_graph, bindings, values)
        assert result["3"]["inputs"]["seed"] == 0

    def test_injects_negative_prompt(self):
        api_graph = {
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "original", "clip": ["4", 1]},
            }
        }
        bindings = {"negative_prompt": {"node_id": "7", "input": "text"}}
        values = {"negative_prompt": "ugly, blurry"}
        result = inject_bindings(api_graph, bindings, values)
        assert result["7"]["inputs"]["text"] == "ugly, blurry"


# ------------------------------------------- workflow registry loading


class TestWorkflowRegistryLoading:
    """load_workflow_registry loads JSON files correctly."""

    def test_loads_example_registry(self):
        registry_path = (
            Path(__file__).resolve().parents[2]
            / "examples"
            / "workflows"
            / "anime_t2i_default.registry.json"
        )
        if not registry_path.exists():
            return  # Skip if example not available.
        registry = load_workflow_registry(registry_path)
        assert registry["workflow_id"] == "anime_t2i_default"
        assert "bindings" in registry
        assert "api_graph" in registry

    def test_raises_on_missing_file(self):
        try:
            load_workflow_registry("/nonexistent/path.json")
            raise AssertionError("should have raised")
        except FileNotFoundError:
            pass


# ------------------------------------------- checkpoint resolution


class TestCheckpointResolution:
    """resolve_checkpoint_path checks standard locations."""

    def test_raises_on_missing_checkpoint(self):
        try:
            resolve_checkpoint_path("nonexistent.safetensors", "/tmp")
            raise AssertionError("should have raised")
        except FileNotFoundError as exc:
            assert "nonexistent.safetensors" in str(exc)
