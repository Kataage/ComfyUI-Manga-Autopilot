"""Tests for the minimal v2 Work database backbone."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.storage import (
    WORK_DATABASE_KIND,
    WORK_FORMAT_VERSION,
    WORK_MIGRATIONS,
    WorkDatabaseIdentityError,
    bootstrap_work_database,
    read_work_identity,
    write_connection,
)


def test_bootstrap_work_database_creates_backbone_without_master_db(tmp_path: Path) -> None:
    database = tmp_path / "works" / "work_001" / "work.sqlite3"
    database.parent.mkdir(parents=True)

    result = bootstrap_work_database(
        database,
        work_id="work_001",
        database_id="workdb_test",
        app_version="test",
    )

    assert database.exists()
    assert not (tmp_path / "master.sqlite3").exists()
    assert result.migration.current_version == WORK_MIGRATIONS[-1].version
    assert result.identity.database_kind == WORK_DATABASE_KIND
    assert result.identity.database_id == "workdb_test"
    assert result.identity.format_version == WORK_FORMAT_VERSION
    assert result.identity.work_id == "work_001"
    assert result.identity.created_at

    with write_connection(database) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert "work_database_metadata" in tables
    assert "commits" in tables
    assert "entity_revisions" in tables
    assert "work_metadata" in tables


def test_bootstrap_work_database_generates_stable_database_id_once(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"

    first = bootstrap_work_database(database, work_id="work_001")
    second = bootstrap_work_database(database, work_id="work_001")

    assert first.identity.database_id.startswith("workdb_")
    assert second.identity.database_id == first.identity.database_id
    assert second.identity.work_id == first.identity.work_id
    assert second.identity.created_at == first.identity.created_at
    assert second.migration.applied_versions == ()


def test_read_work_identity_round_trips_bootstrap_metadata(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    expected = bootstrap_work_database(
        database,
        work_id="work_roundtrip",
        database_id="workdb_roundtrip",
    ).identity

    actual = read_work_identity(database)

    assert actual == expected


def test_bootstrap_rejects_work_id_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_original")

    with pytest.raises(WorkDatabaseIdentityError, match="work_id mismatch"):
        bootstrap_work_database(database, work_id="work_other")

    assert read_work_identity(database).work_id == "work_original"


def test_bootstrap_rejects_database_id_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(
        database,
        work_id="work_001",
        database_id="workdb_original",
    )

    with pytest.raises(WorkDatabaseIdentityError, match="database_id mismatch"):
        bootstrap_work_database(
            database,
            work_id="work_001",
            database_id="workdb_other",
        )


def test_work_commit_sequence_is_monotonic(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_001")

    with write_connection(database) as connection:
        first = connection.execute(
            """
            INSERT INTO commits (
                commit_id,
                actor_type,
                operation_type,
                created_at
            )
            VALUES ('commit_1', 'system', 'create_work', '2026-09-20T00:00:00+00:00')
            """
        )
        second = connection.execute(
            """
            INSERT INTO commits (
                commit_id,
                parent_commit_seq,
                actor_type,
                operation_type,
                created_at
            )
            VALUES (?, ?, 'human', 'edit_work', '2026-09-20T00:00:01+00:00')
            """,
            ("commit_2", int(first.lastrowid)),
        )
        connection.commit()

    assert int(second.lastrowid) > int(first.lastrowid)


def test_work_metadata_can_reference_current_commit_sequence(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_001")

    with write_connection(database) as connection:
        commit = connection.execute(
            """
            INSERT INTO commits (
                commit_id,
                actor_type,
                operation_type,
                created_at
            )
            VALUES ('commit_create', 'system', 'create_work', '2026-09-20T00:00:00+00:00')
            """
        )
        commit_seq = int(commit.lastrowid)
        connection.execute(
            """
            INSERT INTO work_metadata (
                work_id,
                title,
                work_kind,
                language,
                reading_direction,
                status,
                current_commit_seq,
                current_revision,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "work_001",
                "Sample",
                "standalone",
                "ja",
                "RTL_TOP_TO_BOTTOM",
                "DRAFT",
                commit_seq,
                1,
                "2026-09-20T00:00:00+00:00",
                "2026-09-20T00:00:00+00:00",
            ),
        )
        connection.commit()

        row = connection.execute(
            "SELECT * FROM work_metadata WHERE work_id = 'work_001'"
        ).fetchone()

    assert row is not None
    assert row["current_commit_seq"] == commit_seq
    assert row["current_revision"] == 1


def test_work_entity_revision_uniqueness_is_enforced(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_001")

    with write_connection(database) as connection:
        commit = connection.execute(
            """
            INSERT INTO commits (
                commit_id,
                actor_type,
                operation_type,
                created_at
            )
            VALUES ('commit_revision', 'system', 'create_work', '2026-09-20T00:00:00+00:00')
            """
        )
        commit_seq = int(commit.lastrowid)
        connection.execute(
            """
            INSERT INTO entity_revisions (
                id,
                entity_type,
                entity_id,
                entity_revision,
                commit_seq,
                change_kind,
                after_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "revision_1",
                "work",
                "work_001",
                1,
                commit_seq,
                "create",
                '{"title":"Sample"}',
                "2026-09-20T00:00:00+00:00",
            ),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO entity_revisions (
                    id,
                    entity_type,
                    entity_id,
                    entity_revision,
                    commit_seq,
                    change_kind,
                    after_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "revision_2",
                    "work",
                    "work_001",
                    1,
                    commit_seq,
                    "update",
                    '{"title":"Other"}',
                    "2026-09-20T00:00:01+00:00",
                ),
            )


def test_work_entity_revision_requires_existing_commit(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_001")

    with write_connection(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
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
                VALUES ('revision_orphan', 'work', 'work_001', 1, 999, 'create', ?)
                """,
                ("2026-09-20T00:00:00+00:00",),
            )


def test_work_schema_has_no_foreign_key_to_master_tables(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_001")

    with write_connection(database) as connection:
        tables = [
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        ]

        referenced_tables: set[str] = set()
        for table in tables:
            rows = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            referenced_tables.update(str(row["table"]) for row in rows)

    assert not any(name.startswith("master_") for name in referenced_tables)
    assert referenced_tables <= set(tables)
