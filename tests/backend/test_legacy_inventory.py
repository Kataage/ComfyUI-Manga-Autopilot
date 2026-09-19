"""Tests for read-only legacy project discovery and inventory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manga_autopilot.migration.legacy_inventory import (
    LegacyProjectInventoryService,
    LegacyProjectNotFoundError,
)


def _write_project(root: Path, project_id: str = "legacy_001") -> Path:
    project_root = root / "projects" / project_id
    project_root.mkdir(parents=True)
    (project_root / "project.json").write_text(
        json.dumps(
            {
                "id": project_id,
                "name": "legacy-name",
                "title": "Legacy Manga",
                "language": "ja",
                "status": "COMPLETED",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return project_root


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    result: dict[str, tuple[str, bytes | None]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("directory", None)
        elif path.is_symlink():
            result[relative] = ("symlink", None)
        else:
            result[relative] = ("other", None)
    return result


def test_discover_detects_existing_legacy_projects(tmp_path: Path) -> None:
    _write_project(tmp_path, "legacy_b")
    _write_project(tmp_path, "legacy_a")
    (tmp_path / "projects" / "not_a_project").mkdir()
    (tmp_path / "projects" / "plain.txt").write_text("x", encoding="utf-8")

    service = LegacyProjectInventoryService(tmp_path)

    assert service.discover_project_ids() == ("legacy_a", "legacy_b")
    reports = service.discover()
    assert [report.project_id for report in reports] == ["legacy_a", "legacy_b"]


def test_discovery_does_not_create_projects_directory(tmp_path: Path) -> None:
    storage_root = tmp_path / "missing-storage"
    service = LegacyProjectInventoryService(storage_root)

    assert service.discover_project_ids() == ()
    assert not storage_root.exists()


def test_inventory_records_known_files_directories_and_metadata(tmp_path: Path) -> None:
    project_root = _write_project(tmp_path)
    (project_root / "story.json").write_text('{"story":"ok"}', encoding="utf-8")
    (project_root / "characters.json").write_text("[]", encoding="utf-8")
    (project_root / "assets" / "panels").mkdir(parents=True)
    (project_root / "assets" / "panels" / "panel_001.png").write_bytes(b"png")
    (project_root / "jobs").mkdir()
    (project_root / "jobs" / "job_001.json").write_text("{}", encoding="utf-8")
    (project_root / "runs" / "run_001").mkdir(parents=True)
    (project_root / "runs" / "run_001" / "run.json").write_text("{}", encoding="utf-8")
    (project_root / "exports" / "pages").mkdir(parents=True)
    (project_root / "exports" / "pages" / "page_0001.png").write_bytes(b"page")

    report = LegacyProjectInventoryService(tmp_path).inventory_project("legacy_001")

    assert report.project_json_id == "legacy_001"
    assert report.name == "legacy-name"
    assert report.title == "Legacy Manga"

    by_path = {entry.relative_path: entry for entry in report.entries}
    assert by_path["project.json"].category == "project"
    assert by_path["story.json"].category == "story"
    assert by_path["assets/panels/panel_001.png"].category == "assets"
    assert by_path["jobs/job_001.json"].category == "jobs"
    assert by_path["runs/run_001/run.json"].category == "runs"
    assert by_path["exports/pages/page_0001.png"].category == "exports"
    assert all(
        by_path[path].recognized
        for path in (
            "project.json",
            "story.json",
            "assets/panels/panel_001.png",
            "jobs/job_001.json",
            "runs/run_001/run.json",
            "exports/pages/page_0001.png",
        )
    )


def test_missing_optional_files_and_directories_are_reported_not_fabricated(
    tmp_path: Path,
) -> None:
    project_root = _write_project(tmp_path)

    report = LegacyProjectInventoryService(tmp_path).inventory_project("legacy_001")

    assert "story.json" in report.missing_optional_files
    assert "characters.json" in report.missing_optional_files
    assert "assets" in report.missing_optional_directories
    assert "runs" in report.missing_optional_directories
    assert "jobs" in report.missing_optional_directories
    assert "exports" in report.missing_optional_directories

    warning_codes = [warning.code for warning in report.warnings]
    assert "MISSING_OPTIONAL_FILE" in warning_codes
    assert "MISSING_OPTIONAL_DIRECTORY" in warning_codes

    assert not (project_root / "story.json").exists()
    assert not (project_root / "assets").exists()


def test_unknown_files_and_directories_are_retained_in_inventory(tmp_path: Path) -> None:
    project_root = _write_project(tmp_path)
    (project_root / "notes.txt").write_text("editor note", encoding="utf-8")
    (project_root / "custom").mkdir()
    (project_root / "custom" / "plugin-state.bin").write_bytes(b"state")

    report = LegacyProjectInventoryService(tmp_path).inventory_project("legacy_001")

    unknown = {entry.relative_path: entry for entry in report.unknown_entries}
    assert "notes.txt" in unknown
    assert "custom" in unknown
    assert "custom/plugin-state.bin" in unknown
    assert unknown["notes.txt"].recognized is False
    assert unknown["custom/plugin-state.bin"].category == "unknown"


def test_invalid_project_json_is_reported_without_blocking_inventory(tmp_path: Path) -> None:
    project_root = tmp_path / "projects" / "legacy_bad"
    project_root.mkdir(parents=True)
    (project_root / "project.json").write_text("{bad json", encoding="utf-8")
    (project_root / "notes.txt").write_text("keep me", encoding="utf-8")

    report = LegacyProjectInventoryService(tmp_path).inventory_project("legacy_bad")

    assert report.project_json_id is None
    assert any(warning.code == "INVALID_PROJECT_JSON" for warning in report.warnings)
    assert any(entry.relative_path == "notes.txt" for entry in report.unknown_entries)


def test_project_id_mismatch_is_reported_as_ambiguous_source_data(tmp_path: Path) -> None:
    project_root = _write_project(tmp_path, "directory_id")
    payload = json.loads((project_root / "project.json").read_text(encoding="utf-8"))
    payload["id"] = "payload_id"
    (project_root / "project.json").write_text(json.dumps(payload), encoding="utf-8")

    report = LegacyProjectInventoryService(tmp_path).inventory_project("directory_id")

    assert report.project_json_id == "payload_id"
    assert any(warning.code == "PROJECT_ID_MISMATCH" for warning in report.warnings)


def test_inventory_is_read_only(tmp_path: Path) -> None:
    project_root = _write_project(tmp_path)
    (project_root / "story.json").write_text('{"story":"unchanged"}', encoding="utf-8")
    (project_root / "assets").mkdir()
    (project_root / "assets" / "reference.png").write_bytes(b"reference")

    before = _tree_snapshot(project_root)

    report = LegacyProjectInventoryService(tmp_path).inventory_project("legacy_001")

    after = _tree_snapshot(project_root)
    assert report.project_id == "legacy_001"
    assert after == before


def test_report_to_dict_is_importer_friendly_and_json_serializable(tmp_path: Path) -> None:
    project_root = _write_project(tmp_path)
    (project_root / "unknown.bin").write_bytes(b"x")

    report = LegacyProjectInventoryService(tmp_path).inventory_project("legacy_001")
    payload = report.to_dict()

    assert payload["project_id"] == "legacy_001"
    assert isinstance(payload["entries"], list)
    assert isinstance(payload["warnings"], list)
    assert any(
        entry["relative_path"] == "unknown.bin"
        for entry in payload["unknown_entries"]
    )
    json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize(
    "project_id",
    ["", ".", "..", "../escape", "nested/project", r"..\escape"],
)
def test_inventory_rejects_unsafe_project_id(tmp_path: Path, project_id: str) -> None:
    service = LegacyProjectInventoryService(tmp_path)

    with pytest.raises(ValueError):
        service.inventory_project(project_id)


def test_inventory_missing_project_raises_without_creating_it(tmp_path: Path) -> None:
    service = LegacyProjectInventoryService(tmp_path)

    with pytest.raises(LegacyProjectNotFoundError):
        service.inventory_project("missing")

    assert not (tmp_path / "projects" / "missing").exists()


def test_symlink_project_directory_is_not_discovered(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "project.json").write_text('{"id":"linked"}', encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()

    link = projects / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not supported in this environment")

    service = LegacyProjectInventoryService(tmp_path)

    assert "linked" not in service.discover_project_ids()
