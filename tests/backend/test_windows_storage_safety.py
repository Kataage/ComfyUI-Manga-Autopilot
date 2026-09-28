"""Windows-specific persistence and recovery safety coverage."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from manga_autopilot.migration.legacy_inventory import (
    LegacyProjectInventoryService,
)
from manga_autopilot.repositories import (
    WorkIdentityMismatchError,
    WorkLifecycleRepository,
    WorkRecoveryError,
)
from manga_autopilot.storage import (
    WORK_MIGRATIONS,
    Migration,
    UnsafeStoragePathError,
    bootstrap_work_database,
    ensure_storage_root,
    migrate_work_database,
    read_connection,
    write_connection,
)

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows filesystem semantics only",
)


def _create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "failed to create Windows directory junction: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_windows_junction_escape_is_rejected(tmp_path: Path) -> None:
    storage = tmp_path / "store"
    storage.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = storage / "works"
    _create_junction(junction, outside)

    try:
        with pytest.raises(
            UnsafeStoragePathError,
            match="outside managed storage root",
        ):
            ensure_storage_root(storage)
    finally:
        if junction.exists():
            os.rmdir(junction)

    assert outside.is_dir()


def test_windows_unregistered_work_junction_escape_is_invalid(
    tmp_path: Path,
) -> None:
    managed = WorkLifecycleRepository(tmp_path / "managed")
    external = WorkLifecycleRepository(tmp_path / "external")
    external_work = external.create_work(
        work_id="work_windows_junction",
        title="External Work",
    )
    target_manifest = external_work.manifest_path.read_bytes()

    junction = managed.paths.works / external_work.work_id
    _create_junction(junction, external_work.root)

    findings = managed.scan_recovery()

    finding = next(item for item in findings if item.path == junction)
    assert finding.kind == "UNREGISTERED_WORK_INVALID"
    assert finding.valid is False
    assert finding.recommended_action is None
    assert "outside managed storage root" in " ".join(finding.diagnostics)

    with pytest.raises(WorkRecoveryError, match="cannot register invalid orphan"):
        managed.reconcile_orphan_work(external_work.work_id)

    assert external_work.manifest_path.read_bytes() == target_manifest
    assert managed.list_works() == ()


def test_windows_cataloged_work_junction_escape_is_rejected_on_open(
    tmp_path: Path,
) -> None:
    managed = WorkLifecycleRepository(tmp_path / "managed")
    created = managed.create_work(
        work_id="work_windows_catalog_junction",
        title="Managed Work",
    )
    shutil.rmtree(created.root)

    external = WorkLifecycleRepository(tmp_path / "external")
    external_work = external.create_work(
        work_id=created.work_id,
        title="External Work",
    )
    target_manifest = external_work.manifest_path.read_bytes()
    _create_junction(created.root, external_work.root)

    findings = managed.scan_recovery()

    finding = next(item for item in findings if item.work_id == created.work_id)
    assert finding.kind == "CATALOG_WORK_PATH_INVALID"
    assert finding.valid is False
    with pytest.raises(
        WorkIdentityMismatchError,
        match="catalog work path escapes works root",
    ):
        managed.open_work(created.work_id)

    assert external_work.manifest_path.read_bytes() == target_manifest


def test_windows_staging_junction_escape_is_not_finalizable(
    tmp_path: Path,
) -> None:
    managed = WorkLifecycleRepository(tmp_path / "managed")
    external = WorkLifecycleRepository(tmp_path / "external")
    external_work = external.create_work(
        work_id="work_windows_staging_junction",
        title="External Work",
    )
    target_manifest = external_work.manifest_path.read_bytes()

    staging = managed.paths.works / (
        ".creating-" + external_work.work_id
    )
    _create_junction(staging, external_work.root)

    findings = managed.scan_recovery()

    finding = next(item for item in findings if item.path == staging)
    assert finding.kind == "STALE_STAGING_INVALID"
    assert finding.valid is False
    assert finding.recommended_action == "quarantine"
    assert "outside managed storage root" in " ".join(finding.diagnostics)

    with pytest.raises(WorkRecoveryError, match="cannot finalize invalid staging"):
        managed.finalize_staging_work(external_work.work_id)

    assert external_work.manifest_path.read_bytes() == target_manifest


def test_windows_legacy_inventory_does_not_traverse_nested_junction(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    project = storage / "projects" / "legacy_junction"
    project.mkdir(parents=True)
    (project / "project.json").write_text(
        json.dumps({"id": "legacy_junction", "title": "Legacy"}),
        encoding="utf-8",
    )

    outside = tmp_path / "outside-assets"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"do not read or change")
    before = secret.read_bytes()

    assets = project / "assets"
    _create_junction(assets, outside)

    report = LegacyProjectInventoryService(storage).inventory_project(
        "legacy_junction"
    )

    by_path = {entry.relative_path: entry for entry in report.entries}
    assert by_path["assets"].kind == "unsafe_link"
    assert by_path["assets"].recognized is False
    assert "assets/secret.bin" not in by_path
    assert "assets" in report.missing_optional_directories
    assert any(
        warning.code == "UNSAFE_PATH_ENTRY_IGNORED"
        and warning.relative_path == "assets"
        for warning in report.warnings
    )
    assert secret.read_bytes() == before


@pytest.mark.parametrize("critical_name", ["manifest.json", "work.sqlite3"])
def test_windows_open_rejects_non_file_critical_paths(
    tmp_path: Path,
    critical_name: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(
        work_id=f"work_windows_{critical_name.split('.')[0]}",
        title="Windows Critical File",
    )
    critical_path = created.root / critical_name
    critical_path.unlink()
    critical_path.mkdir()

    with pytest.raises(UnsafeStoragePathError, match="regular file"):
        repository.open_work(created.work_id)


def test_windows_invalid_staging_can_be_quarantined_without_deletion(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    staging = repository.paths.works / ".creating-work_windows_quarantine"
    staging.mkdir()
    evidence = staging / "partial.txt"
    evidence.write_text("keep me", encoding="utf-8")

    findings = repository.scan_recovery()

    assert len(findings) == 1
    assert findings[0].kind == "STALE_STAGING_INVALID"
    assert findings[0].recommended_action == "quarantine"

    destination = repository.quarantine_staging_work(
        "work_windows_quarantine",
        reason="windows-ci",
    )

    assert not staging.exists()
    assert (destination / "partial.txt").read_text(encoding="utf-8") == "keep me"
    receipt = destination.with_name(destination.name + ".recovery.json")
    assert receipt.is_file()
    assert repository.scan_recovery() == ()


def test_windows_sqlite_wal_and_migration_backup_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_windows_wal")

    with write_connection(database) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "wal"
        connection.execute("CREATE TABLE wal_probe (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO wal_probe (id) VALUES (1)")
        connection.commit()
        wal = database.with_name(database.name + "-wal")
        assert wal.exists()

    version = WORK_MIGRATIONS[-1].version + 1
    migrations = (
        *WORK_MIGRATIONS,
        Migration(
            version=version,
            name=f"W{version:04d}_windows_ci",
            statements=(
                "CREATE TABLE windows_migration_probe (id INTEGER PRIMARY KEY)",
            ),
        ),
    )

    result = migrate_work_database(
        database,
        migrations=migrations,
        work_id="work_windows_wal",
    )

    assert result.current_version == version
    assert result.backup_path is not None
    assert result.backup_path.is_file()
    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM windows_migration_probe LIMIT 1"
        ).fetchone() is None
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
