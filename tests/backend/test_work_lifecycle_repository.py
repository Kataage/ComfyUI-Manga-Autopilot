"""Integration tests for the v2 Work lifecycle repository."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import manga_autopilot.repositories.work_lifecycle as lifecycle_module
from manga_autopilot.repositories import (
    WorkCreationError,
    WorkIdentityMismatchError,
    WorkLifecycleRepository,
    WorkRecoveryError,
    WorkUpgradeError,
    inspect_work_directory,
)
from manga_autopilot.storage import (
    WORK_MIGRATIONS,
    Migration,
    UnsafeStoragePathError,
    bootstrap_master_database,
    repository_read,
    repository_write,
)


def _future_work_migration(*, broken: bool = False) -> tuple[Migration, ...]:
    version = WORK_MIGRATIONS[-1].version + 1
    statements = (
        ("INSERT INTO missing_upgrade_table (id) VALUES (1)",)
        if broken
        else ("CREATE TABLE upgrade_probe (id INTEGER PRIMARY KEY)",)
    )
    return (
        *WORK_MIGRATIONS,
        Migration(
            version=version,
            name=f"W{version:04d}_test_upgrade",
            statements=statements,
        ),
    )


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
    assert created.schema_version == WORK_MIGRATIONS[-1].version
    assert created.migration_backup_path is None

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
    assert reopened.migration_backup_path is None


@pytest.mark.parametrize(
    "reserved_id",
    [
        ".creating-work_001",
        ".recovery-quarantine",
        ".future-internal",
    ],
)
def test_create_work_rejects_reserved_id_before_work_filesystem_mutation(
    tmp_path: Path,
    reserved_id: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    before = tuple(repository.paths.works.iterdir())

    with pytest.raises(ValueError, match="reserved"):
        repository.create_work(work_id=reserved_id, title="Must Not Create")

    assert tuple(repository.paths.works.iterdir()) == before


@pytest.mark.parametrize(
    "operation",
    [
        "reconcile_orphan_work",
        "finalize_staging_work",
        "quarantine_staging_work",
    ],
)
def test_recovery_operations_reject_reserved_work_ids(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    before = tuple(repository.paths.works.iterdir())

    with pytest.raises(ValueError, match="reserved"):
        getattr(repository, operation)(".creating-not-a-work")

    assert tuple(repository.paths.works.iterdir()) == before


def test_portable_inspection_rejects_reserved_manifest_work_id(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path / "library")
    created = repository.create_work(work_id="work_001", title="Portable")

    portable_root = tmp_path / "portable"
    shutil.copytree(created.root, portable_root)
    manifest_path = portable_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["work_id"] = ".creating-not-a-work"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(lifecycle_module.WorkManifestError, match="reserved"):
        inspect_work_directory(portable_root)


def test_create_work_writes_live_mutable_manifest(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path, app_version="2.0-test")
    handle = repository.create_work(work_id="work_001", title="Manifest Test")

    manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))

    assert manifest["format"] == "manga-autopilot-work"
    assert manifest["format_version"] == 2
    assert manifest["work_id"] == "work_001"
    assert manifest["database"] == "work.sqlite3"
    assert manifest["work_schema_version"] == WORK_MIGRATIONS[-1].version
    assert manifest["created_at"]
    assert manifest["app_version"] == "2.0-test"
    assert manifest["integrity"] == {
        "mode": "live_mutable",
        "database_hash_policy": "package_only",
    }
    assert "database_sha256" not in manifest["integrity"]


def test_legitimate_work_db_edit_does_not_trigger_false_hash_corruption(
    tmp_path: Path,
) -> None:
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
    assert opened.current_revision == 2
    assert listed[0].title == "Catalog Title"


def test_open_does_not_read_legacy_project_json(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    repository.create_work(work_id="work_001", title="V2 Work")

    legacy_dir = tmp_path / "projects" / "work_001"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "project.json").write_text(
        "{ definitely not valid json",
        encoding="utf-8",
    )

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

    def fail_directory_replace(
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
    ) -> None:
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


def test_catalog_registration_failure_preserves_recoverable_orphan_work(
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

    final_root = tmp_path / "works" / "work_fail"
    assert final_root.is_dir()
    assert (final_root / "work.sqlite3").is_file()
    assert (final_root / "manifest.json").is_file()
    assert not (tmp_path / "works" / ".creating-work_fail").exists()

    with repository_read(repository.paths.master_db) as connection:
        row = connection.execute(
            "SELECT 1 FROM work_catalog WHERE work_id = 'work_fail'"
        ).fetchone()
    assert row is None

    findings = repository.scan_recovery()
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "UNREGISTERED_WORK_VALID"
    assert finding.work_id == "work_fail"
    assert finding.valid is True
    assert finding.recommended_action == "register"
    assert finding.diagnostics == ()

    first = repository.reconcile_orphan_work("work_fail")
    second = repository.reconcile_orphan_work("work_fail")

    assert first.work_id == "work_fail"
    assert second == first
    assert repository.open_work("work_fail").title == "Will Fail"
    assert repository.scan_recovery() == ()


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


def test_open_upgrades_work_from_schema_n_to_n_plus_1(tmp_path: Path) -> None:
    old_repository = WorkLifecycleRepository(
        tmp_path,
        app_version="old",
        work_migrations=WORK_MIGRATIONS,
    )
    created = old_repository.create_work(work_id="work_001", title="Upgrade Me")
    old_version = created.schema_version
    future_migrations = _future_work_migration()
    new_version = future_migrations[-1].version

    inspection_before = inspect_work_directory(
        created.root,
        migrations=future_migrations,
    )
    assert inspection_before.database_schema_version == old_version
    assert inspection_before.upgrade_required is True

    new_repository = WorkLifecycleRepository(
        tmp_path,
        app_version="new",
        work_migrations=future_migrations,
    )
    opened = new_repository.open_work("work_001")

    assert opened.schema_version == new_version
    assert opened.migration_backup_path is not None
    assert opened.migration_backup_path.is_file()

    with repository_read(opened.database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'upgrade_probe'"
        ).fetchone() is not None
        actual_version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    manifest = json.loads(opened.manifest_path.read_text(encoding="utf-8"))
    assert actual_version == new_version
    assert manifest["work_schema_version"] == new_version
    assert manifest["format_version"] == 2
    assert manifest["integrity"]["mode"] == "live_mutable"


def test_reopening_already_upgraded_work_is_idempotent(tmp_path: Path) -> None:
    base_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=WORK_MIGRATIONS,
    )
    base_repository.create_work(work_id="work_001", title="Upgrade Once")
    future_migrations = _future_work_migration()

    upgraded_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=future_migrations,
    )
    first = upgraded_repository.open_work("work_001")
    second = upgraded_repository.open_work("work_001")

    assert first.schema_version == future_migrations[-1].version
    assert first.migration_backup_path is not None
    assert second.schema_version == first.schema_version
    assert second.migration_backup_path is None


def test_failed_work_upgrade_does_not_expose_partially_upgraded_work(
    tmp_path: Path,
) -> None:
    base_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=WORK_MIGRATIONS,
    )
    created = base_repository.create_work(work_id="work_001", title="Stay Safe")
    old_manifest = created.manifest_path.read_bytes()
    old_version = created.schema_version
    broken_migrations = _future_work_migration(broken=True)
    target_version = broken_migrations[-1].version

    broken_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=broken_migrations,
    )
    with pytest.raises(WorkUpgradeError, match="failed to upgrade Work"):
        broken_repository.open_work("work_001")

    with repository_read(created.database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    assert version == old_version
    assert created.manifest_path.read_bytes() == old_manifest
    backup = created.database_path.with_name(
        f"{created.database_path.name}.backup-v{old_version}-to-v{target_version}"
    )
    assert backup.is_file()


def test_legacy_v1_live_manifest_is_normalized_without_hash_enforcement(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path, app_version="new")
    created = repository.create_work(work_id="work_001", title="Legacy Manifest")

    legacy_manifest = json.loads(created.manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["format_version"] = 1
    legacy_manifest["integrity"] = {"database_sha256": "0" * 64}
    created.manifest_path.write_text(
        json.dumps(legacy_manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with repository_write(created.database_path) as connection:
        connection.execute(
            """
            UPDATE work_metadata
            SET title = 'Edited After Legacy Hash',
                current_revision = current_revision + 1
            WHERE work_id = 'work_001'
            """
        )

    opened = repository.open_work("work_001")
    normalized = json.loads(opened.manifest_path.read_text(encoding="utf-8"))

    assert opened.title == "Edited After Legacy Hash"
    assert normalized["format_version"] == 2
    assert normalized["integrity"] == {
        "mode": "live_mutable",
        "database_hash_policy": "package_only",
    }


def test_open_repairs_manifest_left_behind_after_completed_db_upgrade(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Refresh Manifest")
    old_manifest = json.loads(created.manifest_path.read_text(encoding="utf-8"))
    future_migrations = _future_work_migration()

    from manga_autopilot.storage import migrate_work_database

    migrate_work_database(
        created.database_path,
        migrations=future_migrations,
    )

    inspection = inspect_work_directory(
        created.root,
        migrations=future_migrations,
    )
    assert inspection.database_schema_version == future_migrations[-1].version
    assert inspection.manifest_schema_version == old_manifest["work_schema_version"]
    assert inspection.manifest_refresh_required is True

    upgraded_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=future_migrations,
    )
    opened = upgraded_repository.open_work("work_001")

    refreshed = json.loads(opened.manifest_path.read_text(encoding="utf-8"))
    assert refreshed["work_schema_version"] == future_migrations[-1].version
    assert opened.migration_backup_path is None


def test_portable_work_inspection_does_not_require_master_database(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path / "library")
    created = repository.create_work(work_id="work_001", title="Portable")

    portable_root = tmp_path / "portable-work"
    shutil.copytree(created.root, portable_root)

    inspection = inspect_work_directory(portable_root)

    assert inspection.work_id == "work_001"
    assert inspection.root == portable_root.resolve()
    assert inspection.database_schema_version == WORK_MIGRATIONS[-1].version
    assert inspection.upgrade_required is False
    assert not (portable_root.parent / "master.sqlite3").exists()



def test_recovery_scan_detects_and_finalizes_complete_stale_staging(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_staged", title="Staged Work")

    staging = tmp_path / "works" / ".creating-work_staged"
    os.replace(created.root, staging)
    with repository_write(repository.paths.master_db) as connection:
        connection.execute(
            "DELETE FROM work_catalog WHERE work_id = 'work_staged'"
        )

    findings = repository.scan_recovery()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "STALE_STAGING_VALID"
    assert finding.work_id == "work_staged"
    assert finding.valid is True
    assert finding.recommended_action == "finalize"
    assert finding.path == staging
    assert finding.diagnostics == ()

    catalog = repository.finalize_staging_work("work_staged")

    assert catalog.work_id == "work_staged"
    assert not staging.exists()
    assert (tmp_path / "works" / "work_staged").is_dir()
    assert repository.open_work("work_staged").title == "Staged Work"
    assert repository.scan_recovery() == ()


def test_recovery_scan_reports_invalid_staging_without_deleting_it(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    staging = tmp_path / "works" / ".creating-work_broken"
    staging.mkdir()
    (staging / "manifest.json").write_text(
        '{"format":"manga-autopilot-work","format_version":2,'
        '"work_id":"work_broken","database":"work.sqlite3",'
        '"work_schema_version":1}',
        encoding="utf-8",
    )
    evidence = staging / "partial.txt"
    evidence.write_text("keep recovery evidence", encoding="utf-8")

    findings = repository.scan_recovery()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "STALE_STAGING_INVALID"
    assert finding.work_id == "work_broken"
    assert finding.valid is False
    assert finding.recommended_action == "quarantine"
    assert finding.diagnostics
    assert staging.is_dir()
    assert evidence.read_text(encoding="utf-8") == "keep recovery evidence"


def test_quarantine_invalid_staging_preserves_evidence_and_is_idempotent_for_scan(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    staging = tmp_path / "works" / ".creating-work_broken"
    staging.mkdir()
    (staging / "partial.txt").write_text("evidence", encoding="utf-8")

    destination = repository.quarantine_staging_work(
        "work_broken",
        reason="incomplete crash state",
    )

    assert not staging.exists()
    assert destination.is_dir()
    assert (destination / "partial.txt").read_text(encoding="utf-8") == "evidence"

    receipt = destination.with_name(destination.name + ".recovery.json")
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["work_id"] == "work_broken"
    assert payload["source"] == ".creating-work_broken"
    assert payload["reason"] == "incomplete crash state"
    assert payload["quarantined_at"]

    assert repository.scan_recovery() == ()


def test_finalize_staging_is_idempotent_after_directory_move(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_retry", title="Retry Finalize")

    staging = tmp_path / "works" / ".creating-work_retry"
    os.replace(created.root, staging)
    with repository_write(repository.paths.master_db) as connection:
        connection.execute("DELETE FROM work_catalog WHERE work_id = 'work_retry'")

    first = repository.finalize_staging_work("work_retry")
    second = repository.finalize_staging_work("work_retry")

    assert first.work_id == "work_retry"
    assert second == first
    assert (tmp_path / "works" / "work_retry").is_dir()
    assert not staging.exists()



def test_repository_rejects_symlinked_master_database_before_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-master.sqlite3"
    bootstrap_master_database(outside, database_id="outside_master")
    before = outside.read_bytes()

    storage = tmp_path / "library"
    (storage / "works").mkdir(parents=True)
    (storage / "shared_assets").mkdir()
    (storage / "projects").mkdir()
    link = storage / "master.sqlite3"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        WorkLifecycleRepository(storage)

    assert outside.read_bytes() == before


def test_open_rejects_symlinked_work_manifest_without_reading_external_file(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Safe")
    outside = tmp_path / "outside-manifest.json"
    outside.write_text(created.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    before = outside.read_bytes()

    created.manifest_path.unlink()
    try:
        created.manifest_path.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        repository.open_work("work_001")

    assert outside.read_bytes() == before


def test_open_rejects_symlinked_work_database_without_mutating_external_db(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Safe")
    outside = tmp_path / "outside-work.sqlite3"
    shutil.copy2(created.database_path, outside)
    before = outside.read_bytes()

    created.database_path.unlink()
    try:
        created.database_path.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        repository.open_work("work_001")

    assert outside.read_bytes() == before


def test_recovery_refuses_orphan_with_symlinked_critical_database(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_orphan", title="Orphan")
    with repository_write(repository.paths.master_db) as connection:
        connection.execute("DELETE FROM work_catalog WHERE work_id = 'work_orphan'")

    outside = tmp_path / "outside-orphan.sqlite3"
    shutil.copy2(created.database_path, outside)
    before = outside.read_bytes()
    created.database_path.unlink()
    try:
        created.database_path.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    findings = repository.scan_recovery()

    assert len(findings) == 1
    assert findings[0].kind == "UNREGISTERED_WORK_INVALID"
    assert findings[0].valid is False
    with pytest.raises(WorkRecoveryError, match="invalid orphan"):
        repository.reconcile_orphan_work("work_orphan")
    assert outside.read_bytes() == before
