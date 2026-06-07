"""Tests for storage path helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from manga_autopilot.storage import (
    ASSET_SUBDIRS,
    EXPORT_SUBDIRS,
    PROJECTS_SUBDIR,
    ensure_project_paths,
    ensure_storage_root,
    project_paths,
)


def test_ensure_storage_root_creates_projects_dir(tmp_path: Path) -> None:
    root = ensure_storage_root(tmp_path / "store")
    assert root.is_dir()
    assert (root / PROJECTS_SUBDIR).is_dir()


def test_project_paths_has_expected_layout(tmp_path: Path) -> None:
    paths = project_paths(tmp_path, "proj_001")
    assert paths.project_id == "proj_001"
    assert paths.root == (tmp_path / PROJECTS_SUBDIR / "proj_001").resolve()
    assert paths.project_json.name == "project.json"
    assert paths.assets.name == "assets"
    assert paths.exports.name == "exports"


def test_ensure_project_paths_creates_all_subdirs(tmp_path: Path) -> None:
    paths = ensure_project_paths(tmp_path, "proj_001")
    assert paths.root.is_dir()
    for sub in ASSET_SUBDIRS:
        assert paths.asset(sub).is_dir()
    for sub in EXPORT_SUBDIRS:
        assert paths.export(sub).is_dir()


def test_project_paths_rejects_empty_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        project_paths(tmp_path, "")


def test_asset_export_helpers_reject_unknown(tmp_path: Path) -> None:
    paths = project_paths(tmp_path, "proj")
    with pytest.raises(ValueError):
        paths.asset("nope")
    with pytest.raises(ValueError):
        paths.export("nope")
