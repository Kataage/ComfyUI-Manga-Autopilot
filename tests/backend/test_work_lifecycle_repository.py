"""Integration tests for the v2 Work lifecycle repository."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import manga_autopilot.repositories.work_lifecycle as lifecycle_module
from manga_autopilot.repositories import (
    WorkCreationError,
    WorkIdentityMismatchError,
    WorkLifecycleRepository,
)
from manga_autopilot.storage import repository_read, repository_write


def test_create_open_list_and_reopen_work(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path, app_version="test")

    created = repository.create_work(
        work_id="work_001",
        title="First Manga",
        work_kind="standalone",
        language="ja",
        reading_direction="RTL_TOP_TO_BOTTOM",
    )

    assert created.work_id == "work_001"
    assert created.title == "First Manga"
    assert created.root == (tmp_path / "works" / "work_001").resolve()
    assert created.database_path.is_file()
    assert created.manifest_path.is_file()
    assert created.current_revision == 1

    listed = repository.list_works()
    assert [entry.work_id for entry in listed] == ["work_001"]
    assert listed[0].relative_work_path == "works/work_001"
    assert listed[0].manifest_hash

    reopened_repository = WorkLifecycleRepository(tmp_path, app_version="test")
    reopened = reopened_repository.open_work("work_001")

    assert reopened.work_id == created.work_id
    assert reopened.title == created.title
    assert reopened.current_commit_seq == created.current_commit_seq
    assert reopened.catalog.last_opened_at is not None


def test_create_work_writes_expected_manifest(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path, app_version="2.0-test")
    handle = repository.create_work(work_id="work_001", title="Manifest Test")

    manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))

    assert manifest["format"] == "manga-autopilot-work"
    assert manifest["format_version"] == 1
    assert manifest["work_id"] == "work_001"
    assert manifest["database"] == "work.sqlite3"
    assert manifest["work_schema_version"] >= 1
    assert manifest["created_at"]
    assert manifest["app_version"] == "2.0-test"
    assert len(manifest["integrity"]["database_sha256"]) == 64


def test_open_uses_work_db_as_authority_not_catalog_title(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Catalog Title")

    with repository_write(created.database_path) as connection:
        connection.execute(
            """
            UPDATE work_metadata
            SET title = 'Authoritative DB Title',
                current_revision = current_revision + 1,
                updated_at = '2026-09-20T01:00:00+00:00'
            WHERE work_id = 'work_001'
            """
        )

    opened = repository.open_work("work_001")
    listed = repository.list_works()

    assert opened.title == "Authoritative DB Title"
    assert listed[0].title == "Catalog Title"


def test_open_does_not_read_legacy_project_json(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    repository.create_work(work_id="work_001", title="V2 Work")

    legacy_dir = tmp_path / "projects" / "work_001"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "project.json").write_text("{ definitely not valid json", encoding="utf-8")

    opened = repository.open_work("work_001")

    assert opened.title == "V2 Work"


def test_open_detects_manifest_work_identity_mismatch(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    handle = repository.create_work(work_id="work_001", title="Identity")

    manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
    manifest["work_id"] = "work_other"
    handle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(WorkIdentityMismatchError, match="manifest Work identity mismatch"):
        repository.open_work("work_001")


def test_open_detects_database_work_identity_mismatch(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    handle = repository.create_work(work_id="work_001", title="Identity")

    with repository_write(handle.database_path) as connection:
        connection.execute(
            """
            UPDATE work_database_metadata
            SET value = 'work_other'
            WHERE key = 'work_id'
            """
        )

    with pytest.raises(WorkIdentityMismatchError, match="Work DB identity mismatch"):
        repository.open_work("work_001")


def test_create_rejects_duplicate_work_id_without_overwrite(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    first = repository.create_work(work_id="work_001", title="First")

    with pytest.raises(WorkCreationError, match="already exists"):
        repository.create_work(work_id="work_001", title="Second")

    assert repository.open_work("work_001").title == "First"
    assert first.database_path.is_file()


def test_create_cleans_staging_on_filesystem_finalize_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    original_replace = os.replace

    def fail_directory_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        source = Path(src)
        if source.name == ".creating-work_fail":
            raise OSError("simulated directory rename failure")
        original_replace(src, dst)

    monkeypatch.setattr(lifecycle_module.os, "replace", fail_directory_replace)

    with pytest.raises(WorkCreationError, match="simulated directory rename failure"):
        repository.create_work(work_id="work_fail", title="Will Fail")

    assert not (tmp_path / "works" / "work_fail").exists()
    assert not (tmp_path / "works" / ".creating-work_fail").exists()
    assert repository.list_works() == ()


def test_catalog_registration_failure_removes_new_finalized_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    original_repository_write = lifecycle_module.repository_write
    master_write_count = 0

    def selective_repository_write(database_path: str | Path):
        nonlocal master_write_count
        if Path(database_path).resolve() == repository.paths.master_db.resolve():
            master_write_count += 1
            if master_write_count == 1:
                raise RuntimeError("simulated catalog write failure")
        return original_repository_write(database_path)

    monkeypatch.setattr(
        lifecycle_module,
        "repository_write",
        selective_repository_write,
    )

    with pytest.raises(WorkCreationError, match="simulated catalog write failure"):
        repository.create_work(work_id="work_fail", title="Will Fail")

    assert not (tmp_path / "works" / "work_fail").exists()
    assert not (tmp_path / "works" / ".creating-work_fail").exists()

    with repository_read(repository.paths.master_db) as connection:
        row = connection.execute(
            "SELECT 1 FROM work_catalog WHERE work_id = 'work_fail'"
        ).fetchone()
    assert row is None


def test_catalog_path_escape_is_rejected(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    repository.create_work(work_id="work_001", title="Safe")

    with repository_write(repository.paths.master_db) as connection:
        connection.execute(
            """
            UPDATE work_catalog
            SET relative_work_path = '../outside'
            WHERE work_id = 'work_001'
            """
        )

    with pytest.raises(WorkIdentityMismatchError, match="unsafe catalog work path"):
        repository.open_work("work_001")


def test_generated_work_id_is_opaque_and_cataloged(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)

    created = repository.create_work(title="Generated ID")

    assert created.work_id.startswith("work_")
    assert "/" not in created.work_id
    assert repository.list_works()[0].work_id == created.work_id
