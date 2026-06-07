"""Tests for the project manager service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manga_autopilot.models import Project
from manga_autopilot.services.project_manager import (
    ProjectManager,
    ProjectNotFoundError,
    generate_project_id,
    validate_project_id,
)


@pytest.fixture()
def manager(tmp_path: Path) -> ProjectManager:
    return ProjectManager(tmp_path / "store")


def test_generate_project_id_uses_safe_slug() -> None:
    pid = generate_project_id("Dark Fantasy Sample!")
    assert pid.startswith("proj_")
    assert "dark_fantasy_sample" in pid


def test_validate_project_id_rejects_unsafe() -> None:
    with pytest.raises(ValueError):
        validate_project_id("../escape")
    with pytest.raises(ValueError):
        validate_project_id("")


def test_create_persists_project_json(manager: ProjectManager) -> None:
    project = manager.create(name="sample", idea="hero rises", title="Sample")
    assert isinstance(project, Project)
    assert project.status == "PROJECT_CREATED"
    paths = manager.paths_for(project.id)
    assert paths.project_json.exists()
    data = json.loads(paths.project_json.read_text(encoding="utf-8"))
    assert data["id"] == project.id
    assert data["title"] == "Sample"


def test_create_rejects_duplicate(manager: ProjectManager) -> None:
    project = manager.create(name="sample")
    with pytest.raises(FileExistsError):
        manager.create(name="sample", project_id=project.id)


def test_save_round_trip(manager: ProjectManager) -> None:
    project = manager.create(name="sample")
    original_updated_at = project.updated_at
    project.title = "Updated"
    project.status = "STORY_PLANNED"
    saved = manager.save(project)
    assert saved.updated_at >= original_updated_at
    loaded = manager.load(project.id)
    assert loaded.title == "Updated"
    assert loaded.status == "STORY_PLANNED"


def test_load_missing_raises(manager: ProjectManager) -> None:
    with pytest.raises(ProjectNotFoundError):
        manager.load("proj_does_not_exist")


def test_list_ids_returns_only_valid_projects(manager: ProjectManager, tmp_path: Path) -> None:
    p1 = manager.create(name="alpha")
    p2 = manager.create(name="beta")
    # create a bogus directory without project.json
    (manager.projects_dir() / "not-a-project").mkdir()
    ids = manager.list_ids()
    assert set(ids) == {p1.id, p2.id}


def test_delete_removes_project(manager: ProjectManager) -> None:
    project = manager.create(name="sample")
    manager.delete(project.id)
    assert not manager.exists(project.id)
    with pytest.raises(ProjectNotFoundError):
        manager.delete(project.id)
