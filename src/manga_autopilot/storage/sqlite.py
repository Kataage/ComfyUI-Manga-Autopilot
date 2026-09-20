"""SQLite connection helpers for v2 persistence."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from manga_autopilot.storage.paths import UnsafeStoragePathError

DEFAULT_BUSY_TIMEOUT_MS = 5_000
REQUIRED_JOURNAL_MODE = "wal"


def _database_path(database_path: str | Path) -> Path:
    raw = Path(database_path).expanduser().absolute()
    if raw.is_symlink():
        raise UnsafeStoragePathError(
            f"database_path must not be a symlink: {raw}"
        )
    path = raw.resolve()
    if path.exists() and path.is_dir():
        raise ValueError(f"database_path must be a file path: {path}")
    return path


def _configure_connection(
    connection: sqlite3.Connection,
    *,
    read_only: bool,
    busy_timeout_ms: int,
) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")

    if read_only:
        connection.execute("PRAGMA query_only = ON")
    else:
        connection.execute(f"PRAGMA journal_mode = {REQUIRED_JOURNAL_MODE}")

    return connection


def connect_write(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a writable SQLite connection configured for v2 persistence."""
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be >= 0")

    path = _database_path(database_path)
    connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1_000)
    try:
        return _configure_connection(
            connection,
            read_only=False,
            busy_timeout_ms=busy_timeout_ms,
        )
    except Exception:
        connection.close()
        raise


def connect_read(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open an existing SQLite database in read-only mode."""
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be >= 0")

    path = _database_path(database_path)
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=busy_timeout_ms / 1_000,
    )
    try:
        return _configure_connection(
            connection,
            read_only=True,
            busy_timeout_ms=busy_timeout_ms,
        )
    except Exception:
        connection.close()
        raise


@contextmanager
def write_connection(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Iterator[sqlite3.Connection]:
    """Yield a configured writable connection and always close it."""
    connection = connect_write(database_path, busy_timeout_ms=busy_timeout_ms)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def read_connection(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> Iterator[sqlite3.Connection]:
    """Yield a configured read-only connection and always close it."""
    connection = connect_read(database_path, busy_timeout_ms=busy_timeout_ms)
    try:
        yield connection
    finally:
        connection.close()


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "REQUIRED_JOURNAL_MODE",
    "connect_read",
    "connect_write",
    "read_connection",
    "write_connection",
]
