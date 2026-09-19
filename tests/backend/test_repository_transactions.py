"""Tests for common v2 repository transaction utilities."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.storage import (
    RevisionConflictError,
    TransactionRequiredError,
    assert_expected_revision,
    bootstrap_master_database,
    bootstrap_work_database,
    connect_write,
    create_master_commit,
    create_work_commit,
    repository_read,
    repository_write,
)


def _seed_work(database: Path) -> None:
    bootstrap_work_database(database, work_id="work_001")
    with repository_write(database) as connection:
        commit = create_work_commit(
            connection,
            commit_id="commit_seed",
            actor_type="system",
            operation_type="create_work",
            created_at="2026-09-20T00:00:00+00:00",
        )
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
                "Initial",
                "standalone",
                "ja",
                "RTL_TOP_TO_BOTTOM",
                "DRAFT",
                commit.commit_seq,
                1,
                "2026-09-20T00:00:00+00:00",
                "2026-09-20T00:00:00+00:00",
            ),
        )


def test_semantic_mutation_commits_commit_and_entity_update_atomically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work.sqlite3"
    _seed_work(database)

    with repository_write(database) as connection:
        row = connection.execute(
            "SELECT current_revision FROM work_metadata WHERE work_id = ?",
            ("work_001",),
        ).fetchone()
        assert row is not None
        assert_expected_revision(
            entity_type="work",
            entity_id="work_001",
            expected_revision=1,
            actual_revision=int(row["current_revision"]),
        )
        commit = create_work_commit(
            connection,
            commit_id="commit_edit",
            parent_commit_seq=1,
            actor_type="human",
            actor_id="user_1",
            operation_type="edit_work",
            reason="rename work",
            created_at="2026-09-20T00:00:01+00:00",
        )
        connection.execute(
            """
            UPDATE work_metadata
            SET title = ?,
                current_commit_seq = ?,
                current_revision = ?,
                updated_at = ?
            WHERE work_id = ?
            """,
            (
                "Updated",
                commit.commit_seq,
                2,
                "2026-09-20T00:00:01+00:00",
                "work_001",
            ),
        )

    with repository_read(database) as connection:
        work = connection.execute(
            """
            SELECT title, current_commit_seq, current_revision
            FROM work_metadata
            WHERE work_id = ?
            """,
            ("work_001",),
        ).fetchone()
        commits = connection.execute(
            "SELECT commit_id FROM commits ORDER BY commit_seq"
        ).fetchall()

    assert work is not None
    assert work["title"] == "Updated"
    assert work["current_revision"] == 2
    assert [row["commit_id"] for row in commits] == ["commit_seed", "commit_edit"]


def test_revision_mismatch_rolls_back_commit_and_partial_mutation(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    _seed_work(database)

    with pytest.raises(RevisionConflictError) as exc_info:
        with repository_write(database) as connection:
            create_work_commit(
                connection,
                commit_id="commit_conflict",
                actor_type="human",
                operation_type="edit_work",
            )
            connection.execute(
                "UPDATE work_metadata SET title = 'Should Roll Back' WHERE work_id = 'work_001'"
            )
            assert_expected_revision(
                entity_type="work",
                entity_id="work_001",
                expected_revision=0,
                actual_revision=1,
            )

    assert exc_info.value.expected_revision == 0
    assert exc_info.value.actual_revision == 1

    with repository_read(database) as connection:
        work = connection.execute(
            "SELECT title, current_revision FROM work_metadata WHERE work_id = 'work_001'"
        ).fetchone()
        conflicting_commit = connection.execute(
            "SELECT 1 FROM commits WHERE commit_id = 'commit_conflict'"
        ).fetchone()

    assert work is not None
    assert work["title"] == "Initial"
    assert work["current_revision"] == 1
    assert conflicting_commit is None


def test_unexpected_exception_rolls_back_transaction(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    _seed_work(database)

    with pytest.raises(RuntimeError, match="boom"):
        with repository_write(database) as connection:
            create_work_commit(
                connection,
                commit_id="commit_boom",
                actor_type="system",
                operation_type="test_failure",
            )
            connection.execute(
                "UPDATE work_metadata SET title = 'Transient' WHERE work_id = 'work_001'"
            )
            raise RuntimeError("boom")

    with repository_read(database) as connection:
        title = connection.execute(
            "SELECT title FROM work_metadata WHERE work_id = 'work_001'"
        ).fetchone()["title"]
        failed_commit = connection.execute(
            "SELECT 1 FROM commits WHERE commit_id = 'commit_boom'"
        ).fetchone()

    assert title == "Initial"
    assert failed_commit is None


def test_create_work_commit_requires_explicit_transaction(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_001")

    connection = connect_write(database)
    try:
        with pytest.raises(TransactionRequiredError):
            create_work_commit(
                connection,
                commit_id="commit_invalid",
                actor_type="system",
                operation_type="invalid",
            )
    finally:
        connection.close()


def test_create_master_commit_uses_same_transaction_boundary(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite3"
    bootstrap_master_database(database, database_id="master_test")

    with repository_write(database) as connection:
        commit = create_master_commit(
            connection,
            commit_id="master_commit_1",
            actor_type="human",
            actor_id="user_1",
            reason="test",
            run_or_operation_id="operation_1",
            created_at="2026-09-20T00:00:00+00:00",
        )

    with repository_read(database) as connection:
        row = connection.execute(
            "SELECT * FROM master_commits WHERE commit_seq = ?",
            (commit.commit_seq,),
        ).fetchone()

    assert row is not None
    assert row["commit_id"] == "master_commit_1"
    assert row["run_or_operation_id"] == "operation_1"


def test_repository_read_is_read_only(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    _seed_work(database)

    with repository_read(database) as connection:
        assert connection.execute(
            "SELECT title FROM work_metadata WHERE work_id = 'work_001'"
        ).fetchone()["title"] == "Initial"

        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE work_metadata SET title = 'Nope' WHERE work_id = 'work_001'"
            )


def test_expected_revision_conflict_contains_entity_context() -> None:
    with pytest.raises(RevisionConflictError) as exc_info:
        assert_expected_revision(
            entity_type="page",
            entity_id="page_001",
            expected_revision=4,
            actual_revision=5,
        )

    error = exc_info.value
    assert error.entity_type == "page"
    assert error.entity_id == "page_001"
    assert error.expected_revision == 4
    assert error.actual_revision == 5
    assert "page_001" in str(error)


def test_matching_expected_revision_is_noop() -> None:
    assert_expected_revision(
        entity_type="panel",
        entity_id="panel_001",
        expected_revision=3,
        actual_revision=3,
    )
