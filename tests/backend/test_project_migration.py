from __future__ import annotations

import json
from pathlib import Path

import pytest

from manga_autopilot.models.project import CURRENT_PROJECT_SCHEMA_VERSION
from manga_autopilot.services.project_manager import ProjectManager
from manga_autopilot.services.project_migration import (
    UnsupportedSchemaVersionError,
    backup_project_document,
    detect_schema_version,
    migrate_project_document,
)

LEGACY_DOCUMENT = {
    "id": "proj-legacy",
    "name": "Legacy project",
    "title": "Old title",
    "idea": "a detective and a cat",
    "language": "ja",
    "status": "PANELS_PLANNED",
    "settings": {
        "page_count": 2,
        "format": ["png_pages"],
        "generation": {
            "candidate_count": 4,
            "max_retry_per_panel": 5,
            "quality_threshold": 0.78,
        },
    },
    "assets": {
        "cover": "assets/pages/cover.png",
        "panels": ["assets/panels/p1_01.png", "assets/panels/p1_02.png"],
    },
    "created_at": "2026-01-02T03:04:05+00:00",
    "updated_at": "2026-01-02T03:04:05+00:00",
}


def _write_legacy_project(tmp_path: Path, project_id: str = "proj-legacy") -> Path:
    root = tmp_path / "projects" / project_id
    root.mkdir(parents=True)
    path = root / "project.json"
    document = dict(LEGACY_DOCUMENT, id=project_id)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_legacy_document_is_detected_as_version_one() -> None:
    assert detect_schema_version(LEGACY_DOCUMENT) == 1
    assert detect_schema_version({"schema_version": 2}) == 2


def test_migration_is_lazy_and_does_not_write(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    result = migrate_project_document(path)

    assert result.migrated is True
    assert (result.from_version, result.to_version) == (1, CURRENT_PROJECT_SCHEMA_VERSION)
    assert result.document["schema_version"] == CURRENT_PROJECT_SCHEMA_VERSION
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    assert list(path.parent.iterdir()) == [path]


def test_migration_records_history_and_preserves_unknown_fields(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)

    document = migrate_project_document(path).document

    assert document["assets"] == LEGACY_DOCUMENT["assets"]
    assert document["settings"] == LEGACY_DOCUMENT["settings"]
    assert document["created_at"] == LEGACY_DOCUMENT["created_at"]
    history = document["migration_history"]
    assert len(history) == 1
    assert (history[0]["from_version"], history[0]["to_version"]) == (
        1,
        CURRENT_PROJECT_SCHEMA_VERSION,
    )
    assert history[0]["migrated_at"].endswith("+00:00")


def test_current_document_is_not_migrated_again(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)
    migrated = migrate_project_document(path).document
    path.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")

    result = migrate_project_document(path)

    assert result.migrated is False
    assert (result.from_version, result.to_version) == (
        CURRENT_PROJECT_SCHEMA_VERSION,
        CURRENT_PROJECT_SCHEMA_VERSION,
    )
    assert result.document["migration_history"] == migrated["migration_history"]


def test_future_schema_version_is_rejected(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)
    path.write_text(json.dumps({"id": "p", "name": "p", "schema_version": 99}), encoding="utf-8")

    with pytest.raises(UnsupportedSchemaVersionError, match="99"):
        migrate_project_document(path)


def test_backup_is_byte_identical_and_timestamped(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)
    original = path.read_bytes()

    backup = backup_project_document(path)

    assert backup.parent == path.parent / "backups"
    assert backup.read_bytes() == original
    assert backup.name.startswith("project.json.")
    assert backup.name.endswith(".bak")
    assert path.read_bytes() == original


def test_repeated_backups_do_not_overwrite_each_other(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)

    first = backup_project_document(path)
    second = backup_project_document(path)

    assert first != second
    assert first.exists() and second.exists()


def test_project_manager_loads_legacy_project_without_touching_the_file(
    tmp_path: Path,
) -> None:
    path = _write_legacy_project(tmp_path)
    before = path.read_bytes()

    project = ProjectManager(tmp_path).load("proj-legacy")

    assert project.schema_version == CURRENT_PROJECT_SCHEMA_VERSION
    assert project.status == "PANELS_PLANNED"
    assert path.read_bytes() == before
    assert not (path.parent / "backups").exists()


def test_first_save_of_a_legacy_project_backs_it_up_then_writes_v2(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)
    original = path.read_bytes()
    manager = ProjectManager(tmp_path)
    project = manager.load("proj-legacy")

    manager.save(project)

    backups = sorted((path.parent / "backups").iterdir())
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == CURRENT_PROJECT_SCHEMA_VERSION
    assert len(saved["migration_history"]) == 1
    assert saved["assets"] == LEGACY_DOCUMENT["assets"]


def test_second_save_does_not_create_another_backup(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)
    manager = ProjectManager(tmp_path)

    project = manager.load("proj-legacy")
    manager.save(project)
    manager.save(manager.load("proj-legacy"))

    assert len(list((path.parent / "backups").iterdir())) == 1


def test_save_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    path = _write_legacy_project(tmp_path)
    manager = ProjectManager(tmp_path)

    manager.save(manager.load("proj-legacy"))

    leftovers = [p.name for p in path.parent.iterdir() if p.is_file() and p.name != "project.json"]
    assert leftovers == []


def test_new_projects_are_written_at_the_current_schema_version(tmp_path: Path) -> None:
    manager = ProjectManager(tmp_path)

    project = manager.create(name="fresh", project_id="proj-fresh")

    document = json.loads(manager.paths_for("proj-fresh").project_json.read_text(encoding="utf-8"))
    assert project.schema_version == CURRENT_PROJECT_SCHEMA_VERSION
    assert document["schema_version"] == CURRENT_PROJECT_SCHEMA_VERSION
    assert document["migration_history"] == []
