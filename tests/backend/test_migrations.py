"""Tests for independent Master/Work SQLite migration runners."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import manga_autopilot.storage.migrations as migrations_module
from manga_autopilot.storage import (
    MASTER_MIGRATIONS,
    SCHEMA_MIGRATIONS_TABLE,
    WORK_MIGRATIONS,
    DatabaseIdentityMismatchError,
    Migration,
    MigrationApplyError,
    MigrationBackupError,
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

    with pytest.raises(
        MigrationValidationError,
        match="post-migration validation",
    ) as exc_info:
        migrate_master_database(
            database,
            migrations=migrations,
            post_integrity_check=fail_validation,
        )

    assert exc_info.value.pending_versions == (next_version,)
    assert exc_info.value.target_version == next_version
    assert [m.name for m in exc_info.value.pending_migrations] == [
        f"M{next_version:04d}_validation_test"
    ]
    assert isinstance(exc_info.value.cause, MigrationIntegrityError)

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



def test_master_bootstrap_rejects_symlink_before_identity_recovery(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-master.sqlite3"
    MigrationRunner(
        database_kind="test",
        migrations=MASTER_MIGRATIONS,
    ).migrate(outside)

    with read_connection(outside) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM master_metadata"
        ).fetchone()[0] == 0
    before = outside.read_bytes()

    managed = tmp_path / "master.sqlite3"
    try:
        managed.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        bootstrap_master_database(
            managed,
            database_id="master_must_not_write",
        )

    assert outside.read_bytes() == before
    with read_connection(outside) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM master_metadata"
        ).fetchone()[0] == 0


def test_work_bootstrap_rejects_symlink_before_identity_recovery(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-work.sqlite3"
    MigrationRunner(
        database_kind="test",
        migrations=WORK_MIGRATIONS,
    ).migrate(outside)

    with read_connection(outside) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM work_database_metadata"
        ).fetchone()[0] == 0
    before = outside.read_bytes()

    managed = tmp_path / "work.sqlite3"
    try:
        managed.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        bootstrap_work_database(
            managed,
            work_id="work_must_not_write",
            database_id="workdb_must_not_write",
        )

    assert outside.read_bytes() == before
    with read_connection(outside) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM work_database_metadata"
        ).fetchone()[0] == 0


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



def test_post_validation_failure_reports_multi_pending_migration_ids(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
    first_version = MASTER_MIGRATIONS[-1].version + 1
    second_version = first_version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=first_version,
            name=f"M{first_version:04d}_first_pending",
            statements=("CREATE TABLE pending_first (id INTEGER PRIMARY KEY)",),
        ),
        Migration(
            version=second_version,
            name=f"M{second_version:04d}_second_pending",
            statements=("CREATE TABLE pending_second (id INTEGER PRIMARY KEY)",),
        ),
    )

    def fail_validation(_connection: sqlite3.Connection) -> None:
        raise MigrationIntegrityError("simulated batch validation failure")

    with pytest.raises(MigrationValidationError) as exc_info:
        migrate_master_database(
            database,
            migrations=migrations,
            post_integrity_check=fail_validation,
        )

    error = exc_info.value
    assert error.pending_versions == (first_version, second_version)
    assert error.target_version == second_version
    assert [migration.name for migration in error.pending_migrations] == [
        f"M{first_version:04d}_first_pending",
        f"M{second_version:04d}_second_pending",
    ]
    assert "pending migrations" in str(error)
    assert str(first_version) in str(error)
    assert str(second_version) in str(error)


def test_backup_failure_reports_pending_migration_ids(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
    first_version = MASTER_MIGRATIONS[-1].version + 1
    second_version = first_version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=first_version,
            name=f"M{first_version:04d}_backup_first",
            statements=("CREATE TABLE backup_first (id INTEGER PRIMARY KEY)",),
        ),
        Migration(
            version=second_version,
            name=f"M{second_version:04d}_backup_second",
            statements=("CREATE TABLE backup_second (id INTEGER PRIMARY KEY)",),
        ),
    )

    backup = database.with_name(
        f"{database.name}.backup-v{MASTER_MIGRATIONS[-1].version}"
        f"-to-v{second_version}"
    )
    outside = tmp_path / "outside-backup-target"
    outside.write_bytes(b"do not replace")
    try:
        backup.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(MigrationBackupError) as exc_info:
        migrate_master_database(database, migrations=migrations)

    error = exc_info.value
    assert error.pending_versions == (first_version, second_version)
    assert error.target_version == second_version
    assert [migration.name for migration in error.pending_migrations] == [
        f"M{first_version:04d}_backup_first",
        f"M{second_version:04d}_backup_second",
    ]
    assert "target version" in str(error)
    assert outside.read_bytes() == b"do not replace"

def test_applied_migrations_must_be_exact_configured_prefix(tmp_path: Path) -> None:
    database = tmp_path / "generic.sqlite3"
    migrations = (
        Migration(version=10, name="first"),
        Migration(version=20, name="second"),
        Migration(version=30, name="third"),
    )
    MigrationRunner(
        database_kind="test",
        migrations=migrations,
    ).migrate(database)

    with write_connection(database) as connection:
        connection.execute(
            f"DELETE FROM {SCHEMA_MIGRATIONS_TABLE} WHERE version = 20"
        )
        connection.commit()

    before = database.read_bytes()
    with pytest.raises(MigrationDriftError, match="ordered configured prefix"):
        MigrationRunner(
            database_kind="test",
            migrations=migrations,
        ).migrate(database)

    assert database.read_bytes() == before
    assert [row["version"] for row in _applied_rows(database)] == [10, 30]


def test_intentionally_sparse_configured_versions_accept_real_prefix(
    tmp_path: Path,
) -> None:
    database = tmp_path / "generic.sqlite3"
    first_two = (
        Migration(version=10, name="first"),
        Migration(version=20, name="second"),
    )
    full = (
        *first_two,
        Migration(version=30, name="third"),
    )
    MigrationRunner(
        database_kind="test",
        migrations=first_two,
    ).migrate(database)

    result = MigrationRunner(
        database_kind="test",
        migrations=full,
    ).migrate(database)

    assert result.applied_versions == (30,)
    assert [row["version"] for row in _applied_rows(database)] == [10, 20, 30]


def test_interrupted_master_identity_recovery_rejects_non_prefix_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    MigrationRunner(
        database_kind="test",
        migrations=MASTER_MIGRATIONS,
    ).migrate(database)

    with write_connection(database) as connection:
        connection.execute(
            f"DELETE FROM {SCHEMA_MIGRATIONS_TABLE} WHERE version = 2"
        )
        connection.commit()

    with pytest.raises(MigrationDriftError, match="ordered configured prefix"):
        bootstrap_master_database(database, database_id="must_not_recover")

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM master_metadata"
        ).fetchone()[0] == 0

def test_existing_master_with_incomplete_identity_is_rejected_before_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
    with write_connection(database) as connection:
        connection.execute(
            "DELETE FROM master_metadata WHERE key = 'database_id'"
        )
        connection.commit()

    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_must_not_run",
            statements=("CREATE TABLE must_not_run (id INTEGER PRIMARY KEY)",),
        ),
    )

    with pytest.raises(UnrecognizedDatabaseError, match="incomplete identity"):
        migrate_master_database(database, migrations=migrations)

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'must_not_run'"
        ).fetchone() is None
    assert not database.with_name(
        f"{database.name}.backup-v{MASTER_MIGRATIONS[-1].version}-to-v{next_version}"
    ).exists()


def test_existing_master_with_wrong_format_is_rejected_before_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
    with write_connection(database) as connection:
        connection.execute(
            "UPDATE master_metadata SET value = '999' WHERE key = 'format_version'"
        )
        connection.commit()

    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_must_not_run",
            statements=("CREATE TABLE must_not_run (id INTEGER PRIMARY KEY)",),
        ),
    )

    with pytest.raises(DatabaseIdentityMismatchError, match="format_version"):
        migrate_master_database(database, migrations=migrations)

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'must_not_run'"
        ).fetchone() is None


def test_existing_work_with_mismatched_work_id_is_rejected_before_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_original")
    next_version = WORK_MIGRATIONS[-1].version + 1
    migrations = (
        *WORK_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"W{next_version:04d}_must_not_run",
            statements=("CREATE TABLE must_not_run (id INTEGER PRIMARY KEY)",),
        ),
    )

    with pytest.raises(DatabaseIdentityMismatchError, match="work_id"):
        migrate_work_database(
            database,
            migrations=migrations,
            work_id="work_other",
        )

    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'must_not_run'"
        ).fetchone() is None
    assert not database.with_name(
        f"{database.name}.backup-v{WORK_MIGRATIONS[-1].version}-to-v{next_version}"
    ).exists()

def test_concurrent_fresh_master_bootstrap_never_deletes_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "master.sqlite3"
    publish_barrier = threading.Barrier(2)
    original_link = migrations_module.os.link

    def synchronized_link(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if Path(destination) == database:
            publish_barrier.wait(timeout=10)
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(migrations_module.os, "link", synchronized_link)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(bootstrap_master_database, database)
            for _ in range(2)
        ]
        results = [future.result(timeout=20) for future in futures]

    identity = read_master_identity(database)
    assert {result.identity.database_id for result in results} == {
        identity.database_id
    }
    assert identity.database_kind == "manga_autopilot_master"
    assert not list(tmp_path.glob(".master.sqlite3.init-*"))


def test_concurrent_fresh_work_bootstrap_never_deletes_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "work.sqlite3"
    publish_barrier = threading.Barrier(2)
    original_link = migrations_module.os.link

    def synchronized_link(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if Path(destination) == database:
            publish_barrier.wait(timeout=10)
        original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(migrations_module.os, "link", synchronized_link)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                bootstrap_work_database,
                database,
                work_id="work_race",
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=20) for future in futures]

    identity = read_work_identity(database)
    assert {result.identity.database_id for result in results} == {
        identity.database_id
    }
    assert identity.work_id == "work_race"
    assert not list(tmp_path.glob(".work.sqlite3.init-*"))


def test_failed_fresh_master_migration_is_retry_safe(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    broken_version = MASTER_MIGRATIONS[-1].version + 1
    broken = (
        *MASTER_MIGRATIONS,
        Migration(
            version=broken_version,
            name=f"M{broken_version:04d}_broken_fresh",
            statements=("INSERT INTO missing_table (id) VALUES (1)",),
        ),
    )

    with pytest.raises(MigrationApplyError):
        migrate_master_database(
            database,
            migrations=broken,
            database_id="master_retry",
        )

    assert not database.exists()
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()

    result = migrate_master_database(
        database,
        database_id="master_retry",
    )
    assert result.current_version == MASTER_MIGRATIONS[-1].version
    assert read_master_identity(database).database_id == "master_retry"


def test_failed_fresh_work_migration_is_retry_safe(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    broken_version = WORK_MIGRATIONS[-1].version + 1
    broken = (
        *WORK_MIGRATIONS,
        Migration(
            version=broken_version,
            name=f"W{broken_version:04d}_broken_fresh",
            statements=("INSERT INTO missing_table (id) VALUES (1)",),
        ),
    )

    with pytest.raises(MigrationApplyError):
        migrate_work_database(
            database,
            migrations=broken,
            work_id="work_retry",
            database_id="workdb_retry",
        )

    assert not database.exists()
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()

    result = migrate_work_database(
        database,
        work_id="work_retry",
        database_id="workdb_retry",
    )
    assert result.current_version == WORK_MIGRATIONS[-1].version
    assert read_work_identity(database).database_id == "workdb_retry"


def test_failed_fresh_migration_never_deletes_preexisting_empty_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "preexisting.sqlite3"
    database.write_bytes(b"")
    broken = (
        Migration(
            version=1,
            name="broken",
            statements=("INSERT INTO missing_table (id) VALUES (1)",),
        ),
    )

    with pytest.raises(MigrationApplyError):
        MigrationRunner(
            database_kind="test",
            migrations=broken,
        ).migrate(database)

    assert database.exists()

def test_current_schema_database_runs_validation_without_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "generic.sqlite3"
    migrations = (Migration(version=1, name="first"),)
    MigrationRunner(
        database_kind="test",
        migrations=migrations,
    ).migrate(database)

    calls: list[str] = []

    def validate(connection: sqlite3.Connection) -> None:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        calls.append("validate")

    result = MigrationRunner(
        database_kind="test",
        migrations=migrations,
        pre_integrity_check=validate,
    ).migrate(database)

    assert calls == ["validate"]
    assert result.applied_versions == ()
    assert result.backup_path is None
    assert not list(tmp_path.glob("*.backup-*"))


def test_current_work_schema_rejects_existing_foreign_key_violation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_001")

    raw = sqlite3.connect(database)
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            """
            INSERT INTO entity_revisions (
                id,
                entity_type,
                entity_id,
                entity_revision,
                commit_seq,
                change_kind,
                created_at
            )
            VALUES (
                'revision_orphan',
                'work',
                'work_001',
                1,
                999,
                'test',
                '2026-09-20T00:00:00+00:00'
            )
            """
        )
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(MigrationIntegrityError, match="foreign_key_check"):
        migrate_work_database(database)

    assert not list(tmp_path.glob("*.backup-*"))


def test_current_schema_integrity_failure_is_not_reported_as_success(
    tmp_path: Path,
) -> None:
    database = tmp_path / "generic.sqlite3"
    migrations = (Migration(version=1, name="first"),)
    MigrationRunner(
        database_kind="test",
        migrations=migrations,
    ).migrate(database)

    def fail_validation(_connection: sqlite3.Connection) -> None:
        raise MigrationIntegrityError("simulated current-schema corruption")

    with pytest.raises(
        MigrationIntegrityError,
        match="simulated current-schema corruption",
    ):
        MigrationRunner(
            database_kind="test",
            migrations=migrations,
            pre_integrity_check=fail_validation,
        ).migrate(database)

def test_backup_temp_directory_failure_keeps_pending_migration_attribution(
    tmp_path: Path,
) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_temp_directory",
            statements=("CREATE TABLE must_not_run (id INTEGER PRIMARY KEY)",),
        ),
    )
    backup = database.with_name(
        f"{database.name}.backup-v{MASTER_MIGRATIONS[-1].version}-to-v{next_version}"
    )
    temp = backup.with_name(backup.name + ".tmp")
    temp.mkdir()

    with pytest.raises(MigrationBackupError) as exc_info:
        migrate_master_database(database, migrations=migrations)

    error = exc_info.value
    assert error.pending_versions == (next_version,)
    assert error.target_version == next_version
    assert isinstance(error.cause, IsADirectoryError)
    assert temp.is_dir()
    assert not backup.exists()
    with read_connection(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'must_not_run'"
        ).fetchone() is None


def test_backup_cleanup_failure_does_not_mask_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")
    next_version = MASTER_MIGRATIONS[-1].version + 1
    migrations = (
        *MASTER_MIGRATIONS,
        Migration(
            version=next_version,
            name=f"M{next_version:04d}_cleanup_mask",
            statements=("CREATE TABLE must_not_run (id INTEGER PRIMARY KEY)",),
        ),
    )
    backup = database.with_name(
        f"{database.name}.backup-v{MASTER_MIGRATIONS[-1].version}-to-v{next_version}"
    )
    temp = backup.with_name(backup.name + ".tmp")
    original_unlink = Path.unlink

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("primary replace failure")

    def fail_temp_unlink(
        self: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if self == temp:
            raise OSError("secondary cleanup failure")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(migrations_module.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)

    with pytest.raises(MigrationBackupError) as exc_info:
        migrate_master_database(database, migrations=migrations)

    error = exc_info.value
    assert error.pending_versions == (next_version,)
    assert error.target_version == next_version
    assert isinstance(error.cause, OSError)
    assert str(error.cause) == "primary replace failure"
    assert temp.exists()
    assert not backup.exists()
