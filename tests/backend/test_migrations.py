"""Tests for independent Master/Work SQLite migration runners."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.storage import (
    MASTER_MIGRATIONS,
    SCHEMA_MIGRATIONS_TABLE,
    WORK_MIGRATIONS,
    DatabaseIdentityMismatchError,
    Migration,
    MigrationApplyError,
    MigrationDriftError,
    MigrationIntegrityError,
    MigrationRunner,
    MigrationValidationError,
    UnknownAppliedMigrationError,
    UnrecognizedDatabaseError,
    UnsafeStoragePathError,
    bootstrap_master_database,
    bootstrap_work_database,
    migrate_master_database,
    migrate_work_database,
    read_connection,
    read_master_identity,
    read_work_identity,
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
    assert result.backup_path is None

    rows = _applied_rows(database)
    assert [row["version"] for row in rows] == [m.version for m in MASTER_MIGRATIONS]
    assert rows[-1]["name"] == MASTER_MIGRATIONS[-1].name
    assert rows[-1]["checksum"] == MASTER_MIGRATIONS[-1].checksum
    assert rows[-1]["app_version"] == "test"
    assert rows[-1]["applied_at"]
    assert read_master_identity(database).database_kind == "manga_autopilot_master"


def test_fresh_work_database_migrates_to_independent_sequence(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"

    result = migrate_work_database(database, work_id="work_direct")

    assert result.database_kind == "work"
    assert result.current_version == WORK_MIGRATIONS[-1].version
    assert result.applied_versions == tuple(m.version for m in WORK_MIGRATIONS)
    assert result.backup_path is None

    rows = _applied_rows(database)
    assert rows[-1]["name"] == WORK_MIGRATIONS[-1].name
    assert rows[-1]["name"] != MASTER_MIGRATIONS[-1].name
    assert read_work_identity(database).work_id == "work_direct"


def test_rerunning_migrations_is_idempotent_for_valid_master(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"

    first = bootstrap_master_database(database).migration
    second = migrate_master_database(database)

    assert first.applied_versions == tuple(m.version for m in MASTER_MIGRATIONS)
    assert second.applied_versions == ()
    assert second.current_version == MASTER_MIGRATIONS[-1].version
    assert second.backup_path is None
    assert len(_applied_rows(database)) == len(MASTER_MIGRATIONS)


def test_failed_fresh_migration_rolls_back_all_pending_schema_changes(
    tmp_path: Path,
) -> None:
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
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert "should_rollback" not in tables
    assert SCHEMA_MIGRATIONS_TABLE not in tables


def test_existing_migration_failure_preserves_verified_backup_and_original(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_broken",
            statements=(
                "CREATE TABLE should_rollback (id INTEGER PRIMARY KEY)",
                "INSERT INTO missing_table (id) VALUES (1)",
            ),
        ),
    )

    with pytest.raises(MigrationApplyError):
        migrate_master_database(database, migrations=migrations)

    backup = database.with_name(
        f"{database.name}.backup-v{MASTER_MIGRATIONS[-1].version}-to-v{next_version}"
    )
    assert backup.is_file()

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'should_rollback'"
        ).fetchone() is None
        version = connection.execute(
            f"SELECT MAX(version) FROM {SCHEMA_MIGRATIONS_TABLE}"
        ).fetchone()[0]
    assert version == MASTER_MIGRATIONS[-1].version

    with read_connection(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        version = connection.execute(
            f"SELECT MAX(version) FROM {SCHEMA_MIGRATIONS_TABLE}"
        ).fetchone()[0]
    assert version == MASTER_MIGRATIONS[-1].version


def test_existing_valid_database_migration_creates_verified_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")

    with write_connection(database) as connection:
        connection.execute(
            """
            INSERT INTO master_commits (commit_id, actor_type, created_at)
            VALUES ('before_backup', 'system', '2026-09-20T00:00:00+00:00')
            """
        )
        connection.commit()

    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_backup_test",
            statements=("CREATE TABLE after_backup (id INTEGER PRIMARY KEY)",),
        ),
    )

    result = migrate_master_database(database, migrations=migrations)

    assert result.backup_path is not None
    assert result.backup_path.is_file()
    assert result.current_version == next_version

    with read_connection(result.backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT 1 FROM master_commits WHERE commit_id = 'before_backup'"
        ).fetchone() is not None
        assert backup.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'after_backup'"
        ).fetchone() is None


def test_post_validation_failure_is_not_recorded_or_committed(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_validation_test",
            statements=("CREATE TABLE validation_test (id INTEGER PRIMARY KEY)",),
        ),
    )

    def fail_validation(_connection: sqlite3.Connection) -> None:
        raise MigrationIntegrityError("simulated post validation failure")

    with pytest.raises(MigrationValidationError, match="post-migration validation"):
        migrate_master_database(
            database,
            migrations=migrations,
            post_integrity_check=fail_validation,
        )

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'validation_test'"
        ).fetchone() is None
        version = connection.execute(
            f"SELECT MAX(version) FROM {SCHEMA_MIGRATIONS_TABLE}"
        ).fetchone()[0]

    assert version == MASTER_MIGRATIONS[-1].version
    backup = database.with_name(
        f"{database.name}.backup-v{MASTER_MIGRATIONS[-1].version}-to-v{next_version}"
    )
    assert backup.is_file()


def test_unknown_nonempty_sqlite_database_is_not_mutated(tmp_path: Path) -> None:
    database = tmp_path / "unknown.sqlite3"
    raw = sqlite3.connect(database)
    try:
        raw.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        raw.execute("INSERT INTO unrelated (id) VALUES (1)")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(UnrecognizedDatabaseError, match="migration history"):
        migrate_master_database(database)

    raw = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        count = raw.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0]
    finally:
        raw.close()

    assert tables == {"unrelated"}
    assert count == 1


def test_wrong_database_identity_is_rejected_before_master_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_001")

    with pytest.raises(
        (DatabaseIdentityMismatchError, MigrationDriftError),
    ):
        migrate_master_database(database)

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'master_metadata'"
        ).fetchone() is None


def test_checksum_drift_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
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
    bootstrap_master_database(database, database_id="master_test")
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

    bootstrap_master_database(master_database, database_id="master_test")
    bootstrap_work_database(work_database, work_id="work_001")

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


def test_pre_and_post_integrity_hooks_are_called_for_fresh_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "generic.sqlite3"
    calls: list[str] = []
    migrations = (Migration(version=1, name="first"),)

    def pre(connection: sqlite3.Connection) -> None:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        calls.append("pre")

    def post(connection: sqlite3.Connection) -> None:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        calls.append("post")

    result = MigrationRunner(
        database_kind="test",
        migrations=migrations,
        pre_integrity_check=pre,
        post_integrity_check=post,
    ).migrate(database)

    assert result.current_version == 1
    assert calls == ["pre", "post"]


def test_pre_integrity_failure_prevents_fresh_migration(tmp_path: Path) -> None:
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



def test_fresh_work_migration_requires_work_id(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"

    with pytest.raises(ValueError, match="work_id is required"):
        migrate_work_database(database)

    assert not database.exists()


def test_generic_runner_can_simulate_stranded_master_identity_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    MigrationRunner(
        database_kind="test",
        migrations=MASTER_MIGRATIONS,
    ).migrate(database)

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM master_metadata"
        ).fetchone()[0] == 0

    recovered = bootstrap_master_database(
        database,
        database_id="master_recovered",
    )

    assert recovered.identity.database_id == "master_recovered"
    assert recovered.identity.database_kind == "manga_autopilot_master"


def test_generic_runner_can_simulate_stranded_work_identity_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work.sqlite3"
    MigrationRunner(
        database_kind="test",
        migrations=WORK_MIGRATIONS,
    ).migrate(database)

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM work_database_metadata"
        ).fetchone()[0] == 0

    recovered = bootstrap_work_database(
        database,
        work_id="work_recovered",
        database_id="workdb_recovered",
    )

    assert recovered.identity.work_id == "work_recovered"
    assert recovered.identity.database_id == "workdb_recovered"



def test_migration_rejects_symlinked_database_before_backup_or_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-master.sqlite3"
    bootstrap_master_database(outside, database_id="master_external")
    before = outside.read_bytes()

    managed = tmp_path / "master.sqlite3"
    try:
        managed.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_must_not_run",
            statements=("CREATE TABLE must_not_exist (id INTEGER PRIMARY KEY)",),
        ),
    )

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        migrate_master_database(managed, migrations=migrations)

    assert outside.read_bytes() == before
    assert not outside.with_name(
        f"{outside.name}.backup-v{MASTER_MIGRATIONS[-1].version}-to-v{next_version}"
    ).exists()
    assert not managed.with_name(
        f"{managed.name}.backup-v{MASTER_MIGRATIONS[-1].version}-to-v{next_version}"
    ).exists()
