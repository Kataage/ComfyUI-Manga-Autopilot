"""Tests for the export service Zip Slip + path restriction fixes."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from manga_autopilot.services.export import ExportService, ProjectImporter
from manga_autopilot.storage.paths import ensure_project_paths


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_zip_slip_absolute_path_is_rejected(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    zip_path = tmp_path / "evil.zip"
    _make_zip(zip_path, {"/etc/passwd": b"hax"})

    importer = ProjectImporter(storage_root=storage)
    with pytest.raises(ValueError, match="absolute path"):
        importer.import_zip(zip_path, project_id="p1")


def test_zip_slip_dotdot_is_rejected(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    zip_path = tmp_path / "evil.zip"
    _make_zip(zip_path, {"../escape.txt": b"hax"})

    importer = ProjectImporter(storage_root=storage)
    with pytest.raises(ValueError, match="escapes the project root"):
        importer.import_zip(zip_path, project_id="p1")


def test_zip_slip_deep_dotdot_is_rejected(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    zip_path = tmp_path / "evil.zip"
    _make_zip(
        zip_path,
        {"ok/file.txt": b"hi", "ok/../../escape.txt": b"hax"},
    )
    importer = ProjectImporter(storage_root=storage)
    with pytest.raises(ValueError, match="escapes the project root"):
        importer.import_zip(zip_path, project_id="p1")


def test_zip_slip_safe_archive_is_imported(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    zip_path = tmp_path / "safe.zip"
    _make_zip(
        zip_path,
        {
            "project.json": b'{"id": "p1"}',
            "story.json": b'{"title": "hi"}',
            "assets/panels/cover.png": b"PNG",
        },
    )
    importer = ProjectImporter(storage_root=storage)
    target = importer.import_zip(zip_path, project_id="p1")
    assert (target / "project.json").read_bytes() == b'{"id": "p1"}'
    assert (target / "assets" / "panels" / "cover.png").read_bytes() == b"PNG"


def test_export_resolve_page_pngs_accepts_within_project(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    paths = ensure_project_paths(storage, "p1")
    page = paths.export("pages") / "page_0001.png"
    page.write_bytes(b"PNG")

    svc = ExportService(storage_root=storage)
    resolved = svc.resolve_page_pngs("p1", [str(page)])
    assert resolved == [page.resolve()]


def test_export_resolve_page_pngs_rejects_outside(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    ensure_project_paths(storage, "p1")
    other = tmp_path / "elsewhere.png"
    other.write_bytes(b"PNG")

    svc = ExportService(storage_root=storage)
    with pytest.raises(ValueError, match="outside the project storage tree"):
        svc.resolve_page_pngs("p1", [str(other)])


def test_export_resolve_page_pngs_rejects_relative(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    ensure_project_paths(storage, "p1")
    svc = ExportService(storage_root=storage)
    with pytest.raises(ValueError, match="must be absolute paths"):
        svc.resolve_page_pngs("p1", ["relative/path.png"])


def test_export_resolve_page_pngs_rejects_missing(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir()
    ensure_project_paths(storage, "p1")
    svc = ExportService(storage_root=storage)
    with pytest.raises(ValueError, match="does not exist"):
        svc.resolve_page_pngs("p1", [str(tmp_path / "ghost.png")])
