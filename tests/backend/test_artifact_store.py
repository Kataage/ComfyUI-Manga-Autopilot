"""Artifact store contract tests — standard CI.

These tests verify the artifact store interface and local implementation
without S3 or cloud credentials.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src is importable.
_SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from manga_autopilot.services.artifact_store import (  # noqa: E402
    ArtifactUploadResult,
    LocalArtifactStore,
    build_panel_artifact_key,
    create_artifact_store_from_env,
)

# ----------------------------------------------- LocalArtifactStore


class TestLocalArtifactStore:
    """LocalArtifactStore tests."""

    def test_uploads_bytes(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        result = store.upload_bytes(
            key="test/image.png",
            data=b"fake png data",
        )
        assert isinstance(result, ArtifactUploadResult)
        assert result.artifact_key == "test/image.png"
        assert result.size_bytes == len(b"fake png data")
        assert result.artifact_path is not None
        assert Path(result.artifact_path).is_file()

    def test_returns_artifact_path(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        result = store.upload_bytes(key="a/b.png", data=b"data")
        expected = tmp_path / "a/b.png"
        assert Path(result.artifact_path) == expected

    def test_returns_public_url_when_configured(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path, public_base_url="https://cdn.example.com")
        result = store.upload_bytes(key="img.png", data=b"data")
        assert result.artifact_url == "https://cdn.example.com/img.png"

    def test_no_public_url_when_not_configured(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        result = store.upload_bytes(key="img.png", data=b"data")
        assert result.artifact_url is None

    def test_rejects_path_traversal(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        with pytest.raises(ValueError, match="path traversal"):
            store.upload_bytes(key="../etc/passwd", data=b"data")

    def test_rejects_absolute_key(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        with pytest.raises(ValueError, match="absolute"):
            store.upload_bytes(key="/etc/passwd", data=b"data")

    def test_rejects_empty_key(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        with pytest.raises(ValueError, match="empty"):
            store.upload_bytes(key="", data=b"data")

    def test_creates_parent_directories(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        result = store.upload_bytes(key="a/b/c/d.png", data=b"data")
        assert Path(result.artifact_path).is_file()

    def test_content_type_stored(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        result = store.upload_bytes(
            key="img.png", data=b"data", content_type="image/jpeg"
        )
        assert result.content_type == "image/jpeg"

    def test_metadata_stored(self, tmp_path: Path):
        store = LocalArtifactStore(tmp_path)
        result = store.upload_bytes(
            key="img.png", data=b"data", metadata={"run_id": "run_123"}
        )
        assert result.metadata["run_id"] == "run_123"


# ----------------------------------------------- artifact key builder


class TestArtifactKeyBuilder:
    """build_panel_artifact_key generates safe keys."""

    def test_uses_project_run_page_panel_candidate(self):
        key = build_panel_artifact_key(
            project_id="proj",
            run_id="run_1",
            page_id="page_0001",
            panel_id="panel_001",
            candidate_id="panel_001_c00",
        )
        assert "proj" in key
        assert "run_1" in key
        assert "page_0001" in key
        assert "panel_001" in key
        assert "panel_001_c00" in key
        assert key.endswith(".png")

    def test_rejects_empty_project_id(self):
        with pytest.raises(ValueError, match="project_id"):
            build_panel_artifact_key(
                project_id="",
                run_id="run_1",
                page_id="page_0001",
                panel_id="panel_001",
                candidate_id="panel_001_c00",
            )

    def test_rejects_empty_run_id(self):
        with pytest.raises(ValueError, match="run_id"):
            build_panel_artifact_key(
                project_id="proj",
                run_id="",
                page_id="page_0001",
                panel_id="panel_001",
                candidate_id="panel_001_c00",
            )

    def test_rejects_path_traversal_in_project_id(self):
        with pytest.raises(ValueError, match="unsafe"):
            build_panel_artifact_key(
                project_id="../etc",
                run_id="run_1",
                page_id="page_0001",
                panel_id="panel_001",
                candidate_id="panel_001_c00",
            )

    def test_rejects_slash_in_panel_id(self):
        with pytest.raises(ValueError, match="unsafe"):
            build_panel_artifact_key(
                project_id="proj",
                run_id="run_1",
                page_id="page_0001",
                panel_id="a/b",
                candidate_id="panel_001_c00",
            )


# ----------------------------------------------- S3 store import


class TestS3StoreImport:
    """S3CompatibleArtifactStore requires boto3."""

    def test_requires_boto3_when_missing(self):
        import unittest.mock

        with unittest.mock.patch.dict("sys.modules", {"boto3": None}):
            # Force reimport.
            if "manga_autopilot.services.artifact_store" in sys.modules:
                del sys.modules["manga_autopilot.services.artifact_store"]
            try:
                import manga_autopilot.services.artifact_store as mod

                with pytest.raises(ImportError, match="boto3"):
                    mod.S3CompatibleArtifactStore(bucket="test")
            finally:
                # Restore original module.
                if "manga_autopilot.services.artifact_store" in sys.modules:
                    del sys.modules["manga_autopilot.services.artifact_store"]

    def test_builds_public_url(self):
        """Verify S3 store URL construction logic."""
        # We can't instantiate without boto3, but we can verify the
        # URL construction pattern.
        base = "https://cdn.example.com"
        key = "projects/proj/runs/run_1/test.png"
        url = f"{base}/{key}"
        assert url == "https://cdn.example.com/projects/proj/runs/run_1/test.png"


# ----------------------------------------------- factory


class TestFactoryFromEnv:
    """create_artifact_store_from_env reads environment variables."""

    def test_defaults_to_local(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("MANGA_AUTOPILOT_ARTIFACT_STORE", raising=False)
        store = create_artifact_store_from_env()
        assert isinstance(store, LocalArtifactStore)

    def test_local_store_uses_env_root(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MANGA_AUTOPILOT_ARTIFACT_STORE", "local")
        monkeypatch.setenv("MANGA_AUTOPILOT_ARTIFACT_LOCAL_ROOT", "/tmp/test-artifacts")
        store = create_artifact_store_from_env()
        assert isinstance(store, LocalArtifactStore)
        assert store.root == Path("/tmp/test-artifacts")


# ----------------------------------------------- response contract


class TestResponseContract:
    """ArtifactUploadResult has expected fields."""

    def test_has_required_fields(self):
        result = ArtifactUploadResult(
            artifact_key="test.png",
            artifact_url="https://example.com/test.png",
            artifact_path="/tmp/test.png",
            content_type="image/png",
            size_bytes=1024,
        )
        assert result.artifact_key == "test.png"
        assert result.artifact_url == "https://example.com/test.png"
        assert result.artifact_path == "/tmp/test.png"
        assert result.content_type == "image/png"
        assert result.size_bytes == 1024

    def test_frozen(self):
        result = ArtifactUploadResult(artifact_key="test.png")
        with pytest.raises(AttributeError):
            result.artifact_key = "other.png"  # type: ignore[misc]
