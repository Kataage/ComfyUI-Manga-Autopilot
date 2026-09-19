"""Tests for the minimal v2 Master database backbone."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.storage import (
    MASTER_DATABASE_KIND,
    MASTER_FORMAT_VERSION,
    MASTER_MIGRATIONS,
    MasterDatabaseIdentityError,
    bootstrap_master_database,
    read_master_identity,
    write_connection,
)


def test_bootstrap_master_database_creates_backbone_and_identity(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"

    result = bootstrap_master_database(
        database,
        database_id="master_test",
        app_version="test",
    )

    assert database.exists()
    assert result.migration.current_version == MASTER_MIGRATIONS[-1].version
    assert result.identity.database_kind == MASTER_DATABASE_KIND
    assert result.identity.database_id == "master_test"
    assert result.identity.format_version == MASTER_FORMAT_VERSION
    assert result.identity.created_at

    with write_connection(database) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

    assert "master_metadata" in tables
    assert "master_commits" in tables
    assert "master_entity_revisions" in tables


def test_bootstrap_master_database_generates_stable_id_once(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"

    first = bootstrap_master_database(database)
    second = bootstrap_master_database(database)

    assert first.identity.database_id.startswith("master_")
    assert second.identity.database_id == first.identity.database_id
    assert second.identity.created_at == first.identity.created_at
    assert second.migration.applied_versions == ()


def test_read_master_identity_round_trips_bootstrap_metadata(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    expected = bootstrap_master_database(database, database_id="master_roundtrip").identity

    actual = read_master_identity(database)

    assert actual == expected


def test_bootstrap_rejects_conflicting_explicit_database_id(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_original")

    with pytest.raises(MasterDatabaseIdentityError, match="database_id mismatch"):
        bootstrap_master_database(database, database_id="master_other")

    assert read_master_identity(database).database_id == "master_original"


def test_bootstrap_rejects_wrong_database_kind(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")

    with write_connection(database) as connection:
        connection.execute(
            "UPDATE master_metadata SET value = 'work' WHERE key = 'database_kind'"
        )
        connection.commit()

    with pytest.raises(MasterDatabaseIdentityError, match="database_kind mismatch"):
        bootstrap_master_database(database)


def test_semantic_master_commit_can_be_inserted_and_queried(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database)

    with write_connection(database) as connection:
        cursor = connection.execute(
            """
            INSERT INTO master_commits (
                commit_id,
                actor_type,
                actor_id,
                reason,
                run_or_operation_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "commit_test_1",
                "human",
                "user_1",
                "create canonical character",
                "operation_1",
                "2026-09-20T00:00:00+00:00",
            ),
        )
        connection.commit()
        commit_seq = int(cursor.lastrowid)

        row = connection.execute(
            """
            SELECT commit_seq, commit_id, actor_type, actor_id, reason,
                   run_or_operation_id, created_at
            FROM master_commits
            WHERE commit_seq = ?
            """,
            (commit_seq,),
        ).fetchone()

    assert row is not None
    assert row["commit_id"] == "commit_test_1"
    assert row["actor_type"] == "human"
    assert row["actor_id"] == "user_1"
    assert row["reason"] == "create canonical character"
    assert row["run_or_operation_id"] == "operation_1"


def test_master_commit_sequence_is_monotonic(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database)

    with write_connection(database) as connection:
        first = connection.execute(
            """
            INSERT INTO master_commits (commit_id, actor_type, created_at)
            VALUES ('commit_1', 'system', '2026-09-20T00:00:00+00:00')
            """
        )
        second = connection.execute(
            """
            INSERT INTO master_commits (commit_id, actor_type, created_at)
            VALUES ('commit_2', 'system', '2026-09-20T00:00:01+00:00')
            """
        )
        connection.commit()

    assert int(second.lastrowid) > int(first.lastrowid)


def test_master_entity_revision_uniqueness_is_enforced(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database)

    with write_connection(database) as connection:
        commit = connection.execute(
            """
            INSERT INTO master_commits (commit_id, actor_type, created_at)
            VALUES ('commit_revision', 'system', '2026-09-20T00:00:00+00:00')
            """
        )
        commit_seq = int(commit.lastrowid)
        revision_values = (
            "revision_1",
            "character",
            "character_1",
            1,
            commit_seq,
            "create",
            None,
            '{"name":"Hero"}',
            "2026-09-20T00:00:00+00:00",
        )
        connection.execute(
            """
            INSERT INTO master_entity_revisions (
                id,
                entity_type,
                entity_id,
                entity_revision,
                commit_seq,
                change_kind,
                before_json,
                after_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            revision_values,
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO master_entity_revisions (
                    id,
                    entity_type,
                    entity_id,
                    entity_revision,
                    commit_seq,
                    change_kind,
                    before_json,
                    after_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "revision_2",
                    "character",
                    "character_1",
                    1,
                    commit_seq,
                    "update",
                    '{"name":"Hero"}',
                    '{"name":"Hero 2"}',
                    "2026-09-20T00:00:01+00:00",
                ),
            )


def test_master_entity_revision_requires_existing_commit(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database)

    with write_connection(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO master_entity_revisions (
                    id,
                    entity_type,
                    entity_id,
                    entity_revision,
                    commit_seq,
                    change_kind,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "revision_orphan",
                    "character",
                    "character_1",
                    1,
                    999,
                    "create",
                    "2026-09-20T00:00:00+00:00",
                ),
            )


def test_bootstrap_upgrades_legacy_master_database_kind_in_place(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")

    with write_connection(database) as connection:
        connection.execute(
            "UPDATE master_metadata SET value = 'master' WHERE key = 'database_kind'"
        )
        connection.commit()

    before = read_master_identity(database)
    assert before.database_kind == MASTER_DATABASE_KIND

    result = bootstrap_master_database(database)

    assert result.identity.database_kind == MASTER_DATABASE_KIND
    with write_connection(database) as connection:
        stored = connection.execute(
            "SELECT value FROM master_metadata WHERE key = 'database_kind'"
        ).fetchone()[0]
    assert stored == MASTER_DATABASE_KIND
