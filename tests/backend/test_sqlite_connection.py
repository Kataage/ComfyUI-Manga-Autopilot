"""Tests for the v2 SQLite connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.storage import (
    DEFAULT_BUSY_TIMEOUT_MS,
    REQUIRED_JOURNAL_MODE,
    UnsafeStoragePathError,
    connect_read,
    connect_write,
    read_connection,
    write_connection,
)


def test_write_connection_applies_required_pragmas(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"

    with write_connection(database) as connection:
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert foreign_keys == 1
    assert journal_mode.lower() == REQUIRED_JOURNAL_MODE
    assert busy_timeout == DEFAULT_BUSY_TIMEOUT_MS


def test_foreign_key_enforcement_rejects_invalid_insert(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"

    with write_connection(database) as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            """
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id)
            )
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")


def test_custom_busy_timeout_is_applied(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"

    with write_connection(database, busy_timeout_ms=1234) as connection:
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert busy_timeout == 1234


@pytest.mark.parametrize("factory", [connect_write, connect_read])
def test_negative_busy_timeout_is_rejected(tmp_path: Path, factory) -> None:
    database = tmp_path / "test.sqlite3"
    if factory is connect_read:
        with write_connection(database):
            pass

    with pytest.raises(ValueError, match="busy_timeout_ms"):
        factory(database, busy_timeout_ms=-1)


def test_read_connection_is_query_only(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"

    with write_connection(database) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample (id, value) VALUES (1, 'ok')")
        connection.commit()

    with read_connection(database) as connection:
        row = connection.execute("SELECT value FROM sample WHERE id = 1").fetchone()
        query_only = connection.execute("PRAGMA query_only").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

        assert row["value"] == "ok"
        assert query_only == 1
        assert foreign_keys == 1
        assert busy_timeout == DEFAULT_BUSY_TIMEOUT_MS

        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO sample (id, value) VALUES (2, 'nope')")


def test_connect_read_does_not_create_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"

    with pytest.raises(sqlite3.OperationalError):
        connect_read(database)

    assert not database.exists()


def test_write_context_manager_closes_connection(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"

    with write_connection(database) as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_read_context_manager_closes_connection(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"

    with write_connection(database):
        pass

    with read_connection(database) as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_connect_write_returns_row_objects(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"
    connection = connect_write(database)
    try:
        row = connection.execute("SELECT 1 AS value").fetchone()
        assert row["value"] == 1
    finally:
        connection.close()



def test_sqlite_helpers_reject_symlinked_database_file(tmp_path: Path) -> None:
    outside = tmp_path / "outside.sqlite3"
    raw = sqlite3.connect(outside)
    try:
        raw.execute("CREATE TABLE sentinel (value TEXT)")
        raw.execute("INSERT INTO sentinel (value) VALUES ('unchanged')")
        raw.commit()
    finally:
        raw.close()
    before = outside.read_bytes()

    managed = tmp_path / "managed.sqlite3"
    try:
        managed.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        connect_read(managed)
    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        connect_write(managed)

    assert outside.read_bytes() == before
