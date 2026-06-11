"""Modal Volume setup helper contract tests — standard CI.

These tests verify the volume setup helpers without Modal SDK or GPU.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure examples/modal-worker is importable.
_EXAMPLES_DIR = str(Path(__file__).resolve().parents[2] / "examples" / "modal-worker")
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

from modal_volume_setup import (  # noqa: E402
    build_modal_volume_put_commands,
    calculate_sha256,
    load_model_manifest,
    validate_model_manifest,
    validate_volume_layout,
)

# --------------------------------------------------- sample data


def _sample_manifest(**overrides: object) -> dict[str, object]:
    base = {
        "version": 1,
        "models": [
            {
                "id": "test-model",
                "type": "checkpoint",
                "filename": "test.safetensors",
                "relative_path": "checkpoints/test.safetensors",
                "required": True,
                "sha256": None,
            },
            {
                "id": "optional-model",
                "type": "vae",
                "filename": "vae.safetensors",
                "relative_path": "vae/vae.safetensors",
                "required": False,
                "sha256": None,
            },
        ],
        "workflows": [
            {
                "workflow_id": "test_workflow",
                "workflow_path": "workflows/test.workflow.json",
                "registry_path": "workflows/test.registry.json",
                "required_models": ["test-model"],
            },
        ],
    }
    base.update(overrides)
    return base


# ----------------------------------------------- manifest loading


class TestManifestLoading:
    """load_model_manifest loads JSON files correctly."""

    def test_loads_example_manifest(self):
        manifest_path = (
            Path(__file__).resolve().parents[2]
            / "examples"
            / "modal-worker"
            / "model_manifest.example.json"
        )
        if not manifest_path.exists():
            return  # Skip if example not available.
        manifest = load_model_manifest(manifest_path)
        assert manifest["version"] == 1
        assert isinstance(manifest["models"], list)
        assert isinstance(manifest["workflows"], list)

    def test_raises_on_missing_file(self):
        try:
            load_model_manifest("/nonexistent/path.json")
            raise AssertionError("should have raised")
        except FileNotFoundError:
            pass


# ----------------------------------------------- manifest validation


class TestManifestValidation:
    """validate_model_manifest checks structure."""

    def test_accepts_valid_manifest(self):
        manifest = _sample_manifest()
        result = validate_model_manifest(manifest)
        assert result["ok"] is True
        assert len(result["errors"]) == 0

    def test_requires_version(self):
        manifest = _sample_manifest()
        del manifest["version"]
        result = validate_model_manifest(manifest)
        assert result["ok"] is False
        assert any("version" in e for e in result["errors"])

    def test_requires_models_list(self):
        manifest = _sample_manifest()
        del manifest["models"]
        result = validate_model_manifest(manifest)
        assert result["ok"] is False
        assert any("models" in e for e in result["errors"])

    def test_requires_workflows_list(self):
        manifest = _sample_manifest()
        del manifest["workflows"]
        result = validate_model_manifest(manifest)
        assert result["ok"] is False
        assert any("workflows" in e for e in result["errors"])

    def test_reports_missing_model_filename(self):
        manifest = _sample_manifest()
        manifest["models"] = [{"id": "bad", "relative_path": "x/y"}]
        result = validate_model_manifest(manifest)
        assert result["ok"] is False
        assert any("filename" in e for e in result["errors"])

    def test_reports_missing_model_path(self):
        manifest = _sample_manifest()
        manifest["models"] = [{"id": "bad", "filename": "x.safetensors"}]
        result = validate_model_manifest(manifest)
        assert result["ok"] is False
        assert any("relative_path" in e for e in result["errors"])


# ----------------------------------------------- volume layout validation


class TestVolumeLayoutValidation:
    """validate_volume_layout checks file existence."""

    def test_accepts_existing_required_checkpoint(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.registry.json").write_bytes(b"{}")

        manifest = _sample_manifest()
        result = validate_volume_layout(tmp_path, manifest)
        assert result["ok"] is True

    def test_reports_missing_required_checkpoint(self, tmp_path: Path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.registry.json").write_bytes(b"{}")

        manifest = _sample_manifest()
        result = validate_volume_layout(tmp_path, manifest)
        assert result["ok"] is False
        assert any("not found" in e for e in result["errors"])

    def test_accepts_missing_optional_model(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.registry.json").write_bytes(b"{}")

        manifest = _sample_manifest()
        result = validate_volume_layout(tmp_path, manifest)
        assert result["ok"] is True

    def test_checks_sha256_when_present(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.registry.json").write_bytes(b"{}")

        import hashlib

        sha = hashlib.sha256(b"data").hexdigest()
        manifest = _sample_manifest()
        manifest["models"][0]["sha256"] = sha

        result = validate_volume_layout(tmp_path, manifest)
        assert result["ok"] is True

    def test_fails_on_sha256_mismatch(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.registry.json").write_bytes(b"{}")

        manifest = _sample_manifest()
        manifest["models"][0]["sha256"] = "wrong_hash"

        result = validate_volume_layout(tmp_path, manifest)
        assert result["ok"] is False
        assert any("sha256 mismatch" in e for e in result["errors"])

    def test_skips_sha256_when_null(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.registry.json").write_bytes(b"{}")

        manifest = _sample_manifest()
        manifest["models"][0]["sha256"] = None

        result = validate_volume_layout(tmp_path, manifest)
        assert result["ok"] is True

    def test_validates_workflow_registry(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.registry.json").write_bytes(b"{}")

        manifest = _sample_manifest()
        result = validate_volume_layout(tmp_path, manifest)
        assert result["ok"] is True

    def test_reports_missing_workflow_registry(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        manifest = _sample_manifest()
        result = validate_volume_layout(tmp_path, manifest)
        assert result["ok"] is False
        assert any("registry not found" in e for e in result["errors"])


# ----------------------------------------------- SHA-256


class TestSha256:
    """calculate_sha256 computes correct hashes."""

    def test_deterministic(self, tmp_path: Path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello world")
        sha1 = calculate_sha256(f)
        sha2 = calculate_sha256(f)
        assert sha1 == sha2

    def test_different_files_differ(self, tmp_path: Path):
        (tmp_path / "a.bin").write_bytes(b"aaa")
        (tmp_path / "b.bin").write_bytes(b"bbb")
        assert calculate_sha256(tmp_path / "a.bin") != calculate_sha256(tmp_path / "b.bin")

    def test_correct_hash(self, tmp_path: Path):
        import hashlib

        f = tmp_path / "test.bin"
        f.write_bytes(b"test data")
        expected = hashlib.sha256(b"test data").hexdigest()
        assert calculate_sha256(f) == expected


# ----------------------------------------------- modal volume put commands


class TestModalVolumePutCommands:
    """build_modal_volume_put_commands generates correct commands."""

    def test_generates_commands_for_models(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        manifest = _sample_manifest()
        commands = build_modal_volume_put_commands(
            "my-volume", manifest, tmp_path
        )
        assert len(commands) >= 1
        assert any("test.safetensors" in cmd for cmd in commands)

    def test_includes_volume_name(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        manifest = _sample_manifest()
        commands = build_modal_volume_put_commands(
            "my-volume", manifest, tmp_path
        )
        assert any("my-volume" in cmd for cmd in commands)

    def test_skips_missing_files(self, tmp_path: Path):
        manifest = _sample_manifest()
        commands = build_modal_volume_put_commands(
            "my-volume", manifest, tmp_path
        )
        assert len(commands) == 0

    def test_includes_workflow_files(self, tmp_path: Path):
        ckpt_dir = tmp_path / "checkpoints"
        ckpt_dir.mkdir()
        (ckpt_dir / "test.safetensors").write_bytes(b"data")

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.registry.json").write_bytes(b"{}")

        manifest = _sample_manifest()
        commands = build_modal_volume_put_commands(
            "my-volume", manifest, tmp_path
        )
        assert any("test.registry.json" in cmd for cmd in commands)


# ----------------------------------------------- response contract


class TestResponseContract:
    """Preflight-style response contract."""

    def test_validate_model_manifest_returns_preflight_format(self):
        manifest = _sample_manifest()
        result = validate_model_manifest(manifest)
        assert "ok" in result
        assert "checks" in result
        assert "errors" in result
        assert isinstance(result["checks"], list)
        assert isinstance(result["errors"], list)

    def test_validate_volume_layout_returns_preflight_format(self, tmp_path: Path):
        manifest = _sample_manifest()
        result = validate_volume_layout(tmp_path, manifest)
        assert "ok" in result
        assert "checks" in result
        assert "errors" in result
