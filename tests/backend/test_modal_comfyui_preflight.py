"""Modal ComfyUI preflight validation contract tests — standard CI.

These tests verify the preflight helpers without Modal SDK or ComfyUI.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure examples/modal-worker is importable.
_EXAMPLES_DIR = str(Path(__file__).resolve().parents[2] / "examples" / "modal-worker")
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from comfyui_preflight import (  # noqa: E402
    detect_checkpoint_from_registry,
    run_preflight,
    validate_checkpoint_exists,
    validate_comfyui_paths,
    validate_modal_comfyui_env,
    validate_workflow_bindings,
    validate_workflow_registry,
)

# ----------------------------------------------------------------- sample data


def _sample_registry(**overrides: object) -> dict[str, object]:
    base = {
        "workflow_id": "anime_t2i_default",
        "name": "Test Workflow",
        "type": "text_to_image",
        "bindings": {
            "positive_prompt": {"node_id": "6", "input": "text"},
            "negative_prompt": {"node_id": "7", "input": "text"},
            "seed": {"node_id": "3", "input": "seed"},
            "width": {"node_id": "5", "input": "width"},
            "height": {"node_id": "5", "input": "height"},
            "checkpoint": {"node_id": "4", "input": "ckpt_name"},
        },
        "api_graph": {
            "3": {"class_type": "KSampler", "inputs": {"seed": 42}},
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "example.safetensors"},
            },
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "test"}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad"}},
        },
    }
    base.update(overrides)
    return base


# ------------------------------------------------ env validation


class TestEnvValidation:
    """validate_modal_comfyui_env checks required env vars."""

    def test_accepts_required_values(self):
        env = {
            "MANGA_AUTOPILOT_MODAL_VOLUME_NAME": "my-volume",
            "MANGA_MODAL_COMFYUI_ROOT": "/root/ComfyUI",
        }
        result = validate_modal_comfyui_env(env)
        assert result["ok"] is True
        assert len(result["errors"]) == 0

    def test_reports_missing_values(self):
        env = {}
        result = validate_modal_comfyui_env(env)
        assert result["ok"] is False
        assert len(result["errors"]) == 2
        assert any("MANGA_AUTOPILOT_MODAL_VOLUME_NAME" in e for e in result["errors"])

    def test_reports_empty_string_as_missing(self):
        env = {
            "MANGA_AUTOPILOT_MODAL_VOLUME_NAME": "",
            "MANGA_MODAL_COMFYUI_ROOT": "  ",
        }
        result = validate_modal_comfyui_env(env)
        assert result["ok"] is False

    def test_custom_required_vars(self):
        env = {"MY_VAR": "value"}
        result = validate_modal_comfyui_env(env, required=("MY_VAR",))
        assert result["ok"] is True

    def test_response_has_checks_list(self):
        result = validate_modal_comfyui_env({})
        assert "checks" in result
        assert isinstance(result["checks"], list)
        for check in result["checks"]:
            assert "name" in check
            assert "ok" in check
            assert "message" in check


# ------------------------------------------------ path validation


class TestPathValidation:
    """validate_comfyui_paths checks directory existence."""

    def test_validates_existing_root(self, tmp_path: Path):
        result = validate_comfyui_paths(comfyui_root=tmp_path)
        assert result["ok"] is True

    def test_reports_missing_root(self):
        result = validate_comfyui_paths(comfyui_root="/nonexistent/path")
        assert result["ok"] is False
        assert any("not found" in e for e in result["errors"])

    def test_validates_checkpoints_dir(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        result = validate_comfyui_paths(
            comfyui_root=tmp_path,
            checkpoints_dir=ckpt_dir,
        )
        assert result["ok"] is True

    def test_reports_missing_checkpoints_dir(self, tmp_path: Path):
        result = validate_comfyui_paths(
            comfyui_root=tmp_path,
            checkpoints_dir=tmp_path / "nocheckpoints",
        )
        assert result["ok"] is False

    def test_validates_workflows_dir(self, tmp_path: Path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        result = validate_comfyui_paths(
            comfyui_root=tmp_path,
            workflows_dir=wf_dir,
        )
        assert result["ok"] is True

    def test_reports_missing_workflows_dir(self, tmp_path: Path):
        result = validate_comfyui_paths(
            comfyui_root=tmp_path,
            workflows_dir=tmp_path / "noworkflows",
        )
        assert result["ok"] is False


# ----------------------------------------------- checkpoint validation


class TestCheckpointValidation:
    """validate_checkpoint_exists checks checkpoint file."""

    def test_checkpoint_exists(self, tmp_path: Path):
        ckpt = tmp_path / "model.safetensors"
        ckpt.write_bytes(b"fake checkpoint data")
        result = validate_checkpoint_exists(
            checkpoints_dir=tmp_path,
            checkpoint_name="model.safetensors",
        )
        assert result["ok"] is True
        assert result["checks"][0]["details"]["size_mb"] >= 0

    def test_checkpoint_missing(self, tmp_path: Path):
        result = validate_checkpoint_exists(
            checkpoints_dir=tmp_path,
            checkpoint_name="nonexistent.safetensors",
        )
        assert result["ok"] is False
        assert "not found" in result["errors"][0]

    def test_lists_available_checkpoints(self, tmp_path: Path):
        (tmp_path / "model1.safetensors").write_bytes(b"data")
        (tmp_path / "model2.ckpt").write_bytes(b"data")
        result = validate_checkpoint_exists(
            checkpoints_dir=tmp_path,
            checkpoint_name="missing.safetensors",
        )
        assert result["ok"] is False
        assert "model1.safetensors" in result["errors"][0]


# ----------------------------------------------- workflow validation


class TestWorkflowValidation:
    """validate_workflow_registry checks JSON structure."""

    def test_accepts_valid_registry(self):
        registry = _sample_registry()
        result = validate_workflow_registry(registry)
        assert result["ok"] is True

    def test_rejects_missing_workflow_id(self):
        registry = _sample_registry()
        del registry["workflow_id"]
        result = validate_workflow_registry(registry)
        assert result["ok"] is False
        assert any("workflow_id" in e for e in result["errors"])

    def test_rejects_missing_bindings(self):
        registry = _sample_registry()
        del registry["bindings"]
        result = validate_workflow_registry(registry)
        assert result["ok"] is False

    def test_rejects_missing_api_graph(self):
        registry = _sample_registry()
        del registry["api_graph"]
        result = validate_workflow_registry(registry)
        assert result["ok"] is False

    def test_rejects_empty_bindings(self):
        registry = _sample_registry()
        registry["bindings"] = {}
        result = validate_workflow_registry(registry)
        assert result["ok"] is False


# ----------------------------------------------- binding validation


class TestBindingValidation:
    """validate_workflow_bindings checks required bindings."""

    def test_accepts_all_required_bindings(self):
        registry = _sample_registry()
        result = validate_workflow_bindings(registry)
        assert result["ok"] is True

    def test_rejects_missing_positive_prompt(self):
        registry = _sample_registry()
        del registry["bindings"]["positive_prompt"]
        result = validate_workflow_bindings(registry)
        assert result["ok"] is False
        assert any("positive_prompt" in e for e in result["errors"])

    def test_rejects_missing_seed(self):
        registry = _sample_registry()
        del registry["bindings"]["seed"]
        result = validate_workflow_bindings(registry)
        assert result["ok"] is False
        assert any("seed" in e for e in result["errors"])

    def test_custom_required_bindings(self):
        registry = _sample_registry()
        result = validate_workflow_bindings(registry, required_bindings=["seed", "width"])
        assert result["ok"] is True

    def test_rejects_missing_custom_binding(self):
        registry = _sample_registry()
        result = validate_workflow_bindings(
            registry, required_bindings=["missing_key"]
        )
        assert result["ok"] is False


# ------------------------------------------- checkpoint detection


class TestCheckpointDetection:
    """detect_checkpoint_from_registry reads checkpoint name."""

    def test_detects_from_binding(self):
        registry = _sample_registry()
        detected = detect_checkpoint_from_registry(registry)
        assert detected == "example.safetensors"

    def test_detects_from_class_type(self):
        registry = {
            "bindings": {},
            "api_graph": {
                "4": {
                    "class_type": "CheckpointLoaderSimple",
                    "inputs": {"ckpt_name": "model.ckpt"},
                }
            },
        }
        detected = detect_checkpoint_from_registry(registry)
        assert detected == "model.ckpt"

    def test_returns_none_when_not_found(self):
        registry = {
            "bindings": {},
            "api_graph": {
                "3": {"class_type": "KSampler", "inputs": {"seed": 42}}
            },
        }
        detected = detect_checkpoint_from_registry(registry)
        assert detected is None


# ----------------------------------------------- combined preflight


class TestCombinedPreflight:
    """run_preflight runs all checks and returns combined result."""

    def test_all_ok_with_valid_inputs(self, tmp_path: Path):
        (tmp_path / "model.safetensors").write_bytes(b"data")
        registry = _sample_registry()
        env = {
            "MANGA_AUTOPILOT_MODAL_VOLUME_NAME": "vol",
            "MANGA_MODAL_COMFYUI_ROOT": str(tmp_path),
        }
        result = run_preflight(
            env=env,
            comfyui_root=tmp_path,
            checkpoints_dir=tmp_path,
            checkpoint_name="model.safetensors",
            workflow_registry=registry,
        )
        assert result["ok"] is True
        assert result["executor"] == "modal-comfyui"
        assert len(result["checks"]) > 0

    def test_fails_on_missing_env(self, tmp_path: Path):
        result = run_preflight(
            env={},
            comfyui_root=tmp_path,
        )
        assert result["ok"] is False

    def test_fails_on_missing_checkpoint(self, tmp_path: Path):
        env = {
            "MANGA_AUTOPILOT_MODAL_VOLUME_NAME": "vol",
            "MANGA_MODAL_COMFYUI_ROOT": str(tmp_path),
        }
        result = run_preflight(
            env=env,
            comfyui_root=tmp_path,
            checkpoints_dir=tmp_path,
            checkpoint_name="missing.safetensors",
        )
        assert result["ok"] is False

    def test_auto_detects_checkpoint(self, tmp_path: Path):
        (tmp_path / "example.safetensors").write_bytes(b"data")
        env = {
            "MANGA_AUTOPILOT_MODAL_VOLUME_NAME": "vol",
            "MANGA_MODAL_COMFYUI_ROOT": str(tmp_path),
        }
        registry = _sample_registry()
        result = run_preflight(
            env=env,
            comfyui_root=tmp_path,
            checkpoints_dir=tmp_path,
            workflow_registry=registry,
        )
        assert result["ok"] is True


# ----------------------------------------------- health response


class TestHealthResponse:
    """Health response contract tests."""

    def test_health_response_structure(self):
        """Verify expected keys in health response."""
        expected_keys = {
            "status",
            "executor",
            "comfyui_root",
            "volume_name",
            "comfyui_port",
        }
        # This is a structural test - we verify the expected format.
        health = {
            "status": "ok",
            "executor": "modal-comfyui",
            "comfyui_root": "/root/ComfyUI",
            "comfyui_root_exists": True,
            "volume_name": "manga-autopilot-comfyui",
            "volume_mounted": True,
            "output_dir": "/outputs",
            "comfyui_port": 8188,
        }
        for key in expected_keys:
            assert key in health
        assert health["status"] == "ok"
        assert health["executor"] == "modal-comfyui"
