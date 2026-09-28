"""Windows-specific persistence and recovery safety coverage."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from manga_autopilot.repositories import WorkLifecycleRepository
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
