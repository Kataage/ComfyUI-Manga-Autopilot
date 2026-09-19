"""Tests for independent Master/Work SQLite migration runners."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.storage import (
    MASTER_MIGRATIONS,
    SCHEMA_MIGRATIONS_TABLE,
    WORK_MIGRATIONS,
    Migration,
    MigrationApplyError,
    MigrationDriftError,
    MigrationIntegrityError,
    MigrationRunner,
    UnknownAppliedMigrationError,
    migrate_master_database,
    migrate_work_database,
    write_connection,
)


def _applied_rows(database: Path) -> list[sqlite3.Row]:
    with write_connection(database) as connection:
        return connection.execute(
            f"""
            SELECT version, name, checksum, applied_at, app_version
            FROM {SCHEMA_MIGRATIONS_TABLE}
            ORDER BY version
            """
        ).fetchall()


def test_fresh_master_database_migrates_to_latest(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"

    result = migrate_master_database(database, app_version="test")

    assert result.database_kind == "master"
    assert result.current_version == MASTER_MIGRATIONS[-1].version
    assert result.applied_versions == tuple(m.version for m in MASTER_MIGRATIONS)

    rows = _applied_rows(database)
    assert [row["version"] for row in rows] == [m.version for m in MASTER_MIGRATIONS]
    assert rows[-1]["name"] == MASTER_MIGRATIONS[-1].name
    assert rows[-1]["checksum"] == MASTER_MIGRATIONS[-1].checksum
    assert rows[-1]["app_version"] == "test"
    assert rows[-1]["applied_at"]


def test_fresh_work_database_migrates_to_independent_sequence(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"

    result = migrate_work_database(database)

    assert result.database_kind == "work"
    assert result.current_version == WORK_MIGRATIONS[-1].version
    assert result.applied_versions == tuple(m.version for m in WORK_MIGRATIONS)

    rows = _applied_rows(database)
    assert rows[-1]["name"] == WORK_MIGRATIONS[-1].name
    assert rows[-1]["name"] != MASTER_MIGRATIONS[-1].name


def test_rerunning_migrations_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"

    first = migrate_master_database(database)
    second = migrate_master_database(database)

    assert first.applied_versions == tuple(m.version for m in MASTER_MIGRATIONS)
    assert second.applied_versions == ()
    assert second.current_version == MASTER_MIGRATIONS[-1].version
    assert len(_applied_rows(database)) == len(MASTER_MIGRATIONS)


def test_failed_migration_rolls_back_its_schema_changes(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_broken",
            statements=(
                "CREATE TABLE should_rollback (id INTEGER PRIMARY KEY)",
                "INSERT INTO table_that_does_not_exist (id) VALUES (1)",
            ),
        ),
    )

    with pytest.raises(MigrationApplyError) as exc_info:
        migrate_master_database(database, migrations=migrations)

    assert exc_info.value.migration.version == next_version

    with write_connection(database) as connection:
        table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'should_rollback'
            """
        ).fetchone()
        versions = connection.execute(
            f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY version"
        ).fetchall()

    assert table is None
    assert [row["version"] for row in versions] == [m.version for m in MASTER_MIGRATIONS]


def test_checksum_drift_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    next_version = MASTER_MIGRATIONS[-1].version + 1
    original = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_example",
            statements=("CREATE TABLE sample (id INTEGER PRIMARY KEY)",),
        ),
    )
    changed = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_example",
            statements=(
                "CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)",
            ),
        ),
    )

    migrate_master_database(database, migrations=original)

    with pytest.raises(MigrationDriftError, match="drift detected"):
        migrate_master_database(database, migrations=changed)


def test_unknown_applied_migration_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    next_version = MASTER_MIGRATIONS[-1].version + 1
    extended = (
        *MASTER_MIGRATIONS,
        Migration(version=next_version, name=f"M{next_version:04d}_known_now"),
    )
    migrate_master_database(database, migrations=extended)

    with pytest.raises(UnknownAppliedMigrationError, match="unknown applied"):
        migrate_master_database(database, migrations=MASTER_MIGRATIONS)


def test_master_and_work_versions_can_advance_independently(tmp_path: Path) -> None:
    master_database = tmp_path / "master.sqlite3"
    work_database = tmp_path / "work.sqlite3"

    next_version = MASTER_MIGRATIONS[-1].version + 1
    master_migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_master_only",
            statements=("CREATE TABLE master_only (id INTEGER PRIMARY KEY)",),
        ),
    )

    master_result = migrate_master_database(
        master_database,
        migrations=master_migrations,
    )
    work_result = migrate_work_database(work_database)

    assert master_result.current_version == next_version
    assert work_result.current_version == WORK_MIGRATIONS[-1].version


def test_pre_and_post_integrity_hooks_are_called(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    calls: list[str] = []

    def pre(connection: sqlite3.Connection) -> None:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        calls.append("pre")

    def post(connection: sqlite3.Connection) -> None:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        calls.append("post")

    result = migrate_master_database(
        database,
        pre_integrity_check=pre,
        post_integrity_check=post,
    )

    assert result.current_version == MASTER_MIGRATIONS[-1].version
    assert calls == ["pre", "post"]


def test_pre_integrity_failure_prevents_migration(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"

    def fail(_connection: sqlite3.Connection) -> None:
        raise MigrationIntegrityError("pre-check failed")

    with pytest.raises(MigrationIntegrityError, match="pre-check failed"):
        migrate_master_database(database, pre_integrity_check=fail)

    with write_connection(database) as connection:
        table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (SCHEMA_MIGRATIONS_TABLE,),
        ).fetchone()

    assert table is None


def test_runner_rejects_out_of_order_migrations() -> None:
    with pytest.raises(ValueError, match="ascending"):
        MigrationRunner(
            database_kind="test",
            migrations=(
                Migration(version=2, name="second"),
                Migration(version=1, name="first"),
            ),
        )


def test_runner_rejects_duplicate_versions() -> None:
    with pytest.raises(ValueError, match="versions must be unique"):
        MigrationRunner(
            database_kind="test",
            migrations=(
                Migration(version=1, name="first"),
                Migration(version=1, name="duplicate"),
            ),
        )


def test_migration_checksum_is_stable() -> None:
    first = Migration(
        version=2,
        name="M0002_example",
        statements=("CREATE TABLE sample (id INTEGER PRIMARY KEY)",),
    )
    second = Migration(
        version=2,
        name="M0002_example",
        statements=("CREATE TABLE sample (id INTEGER PRIMARY KEY)",),
    )

    assert first.checksum == second.checksum
