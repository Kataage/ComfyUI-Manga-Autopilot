"""Tests for storage path helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from manga_autopilot.storage import (
    ASSET_SUBDIRS,
    EXPORT_SUBDIRS,
    MASTER_DB_FILENAME,
    PROJECTS_SUBDIR,
    SHARED_ASSETS_SUBDIR,
    WORK_DB_FILENAME,
    WORK_MANIFEST_FILENAME,
    WORKS_SUBDIR,
    UnsafeStoragePathError,
    ensure_legacy_project_paths,
    ensure_project_paths,
    ensure_storage_root,
    ensure_work_paths,
    legacy_project_paths,
    project_paths,
    storage_paths,
    work_paths,
)


def test_ensure_storage_root_creates_v2_and_legacy_top_level_dirs(tmp_path: Path) -> None:
    root = ensure_storage_root(tmp_path / "store")
    assert root.is_dir()
    assert (root / SHARED_ASSETS_SUBDIR).is_dir()
    assert (root / WORKS_SUBDIR).is_dir()
    assert (root / PROJECTS_SUBDIR).is_dir()
    assert not (root / MASTER_DB_FILENAME).exists()


def test_storage_paths_has_expected_v2_layout(tmp_path: Path) -> None:
    paths = storage_paths(tmp_path / "store")
    assert paths.root == (tmp_path / "store").resolve()
    assert paths.master_db == paths.root / MASTER_DB_FILENAME
    assert paths.shared_assets == paths.root / SHARED_ASSETS_SUBDIR
    assert paths.works == paths.root / WORKS_SUBDIR
    assert paths.legacy_projects == paths.root / PROJECTS_SUBDIR


def test_work_paths_has_expected_layout(tmp_path: Path) -> None:
    paths = work_paths(tmp_path, "work_001")
    assert paths.work_id == "work_001"
    assert paths.root == (tmp_path / WORKS_SUBDIR / "work_001").resolve()
    assert paths.manifest_json == paths.root / WORK_MANIFEST_FILENAME
    assert paths.work_db == paths.root / WORK_DB_FILENAME
    assert paths.assets == paths.root / "assets"
    assert paths.cache == paths.root / "cache"
    assert paths.exports == paths.root / "exports"


def test_ensure_work_paths_creates_directories_but_not_db_or_manifest(tmp_path: Path) -> None:
    paths = ensure_work_paths(tmp_path, "work_001")
    assert paths.root.is_dir()
    assert paths.assets.is_dir()
    assert paths.cache.is_dir()
    assert paths.exports.is_dir()
    assert not paths.work_db.exists()
    assert not paths.manifest_json.exists()


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "",
        ".",
        "..",
        "../escape",
        "nested/work",
        r"..\escape",
        "/absolute",
    ],
)
def test_work_paths_rejects_path_traversal(tmp_path: Path, unsafe_id: str) -> None:
    with pytest.raises(ValueError):
        work_paths(tmp_path, unsafe_id)


@pytest.mark.parametrize(
    "reserved_id",
    [
        ".creating-work_001",
        ".recovery-quarantine",
        ".internal-future",
    ],
)
def test_work_paths_rejects_reserved_internal_namespace(
    tmp_path: Path,
    reserved_id: str,
) -> None:
    with pytest.raises(ValueError, match="reserved"):
        work_paths(tmp_path, reserved_id)


def test_legacy_project_paths_has_expected_layout(tmp_path: Path) -> None:
    paths = legacy_project_paths(tmp_path, "proj_001")
    assert paths.project_id == "proj_001"
    assert paths.root == (tmp_path / PROJECTS_SUBDIR / "proj_001").resolve()
    assert paths.project_json.name == "project.json"
    assert paths.assets.name == "assets"
    assert paths.exports.name == "exports"


def test_project_paths_remains_legacy_compatibility_alias(tmp_path: Path) -> None:
    explicit = legacy_project_paths(tmp_path, "proj_001")
    compatibility = project_paths(tmp_path, "proj_001")
    assert compatibility == explicit


def test_ensure_legacy_project_paths_creates_all_subdirs(tmp_path: Path) -> None:
    paths = ensure_legacy_project_paths(tmp_path, "proj_001")
    assert paths.root.is_dir()
    for sub in ASSET_SUBDIRS:
        assert paths.asset(sub).is_dir()
    for sub in EXPORT_SUBDIRS:
        assert paths.export(sub).is_dir()


def test_ensure_project_paths_remains_legacy_compatibility_wrapper(tmp_path: Path) -> None:
    paths = ensure_project_paths(tmp_path, "proj_001")
    assert paths.project_json.parent == (tmp_path / PROJECTS_SUBDIR / "proj_001").resolve()
    for sub in ASSET_SUBDIRS:
        assert paths.asset(sub).is_dir()
    for sub in EXPORT_SUBDIRS:
        assert paths.export(sub).is_dir()


@pytest.mark.parametrize("unsafe_id", ["", "..", "../escape", "nested/project", r"..\escape"])
def test_legacy_project_paths_rejects_path_traversal(tmp_path: Path, unsafe_id: str) -> None:
    with pytest.raises(ValueError):
        legacy_project_paths(tmp_path, unsafe_id)


def test_legacy_run_dir_rejects_path_traversal(tmp_path: Path) -> None:
    paths = project_paths(tmp_path, "proj")
    with pytest.raises(ValueError):
        paths.run_dir("../run")


def test_asset_export_helpers_reject_unknown(tmp_path: Path) -> None:
    paths = project_paths(tmp_path, "proj")
    with pytest.raises(ValueError):
        paths.asset("nope")
    with pytest.raises(ValueError):
        paths.export("nope")



def test_ensure_storage_root_rejects_symlinked_works_directory(tmp_path: Path) -> None:
    storage = tmp_path / "store"
    storage.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    works_link = storage / WORKS_SUBDIR
    try:
        works_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        ensure_storage_root(storage)


def test_ensure_work_paths_rejects_existing_work_symlink_escape(tmp_path: Path) -> None:
    storage = tmp_path / "store"
    ensure_storage_root(storage)
    outside = tmp_path / "outside-work"
    outside.mkdir()
    link = storage / WORKS_SUBDIR / "work_001"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        ensure_work_paths(storage, "work_001")

    assert not (outside / "assets").exists()
    assert not (outside / "cache").exists()
    assert not (outside / "exports").exists()


def test_ensure_legacy_project_paths_rejects_project_symlink_escape(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "store"
    ensure_storage_root(storage)
    outside = tmp_path / "outside-project"
    outside.mkdir()
    link = storage / PROJECTS_SUBDIR / "proj_001"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        ensure_legacy_project_paths(storage, "proj_001")

    assert not (outside / "assets").exists()
    assert not (outside / "exports").exists()


def test_ensure_legacy_project_paths_rejects_nested_assets_symlink(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "store"
    ensure_storage_root(storage)
    project = storage / PROJECTS_SUBDIR / "proj_001"
    project.mkdir()
    outside = tmp_path / "outside-assets"
    outside.mkdir()
    assets_link = project / "assets"
    try:
        assets_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        ensure_legacy_project_paths(storage, "proj_001")

    assert not (outside / "characters").exists()
    assert not (outside / "panels").exists()
