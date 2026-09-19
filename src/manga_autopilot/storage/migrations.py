"""Versioned SQLite migration runner for Master and Work databases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manga_autopilot.storage.sqlite import write_connection

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"

_SCHEMA_MIGRATIONS_SQL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    app_version TEXT
)
"""


class MigrationError(RuntimeError):
    """Base class for migration failures."""


class MigrationDriftError(MigrationError):
    """Raised when an applied migration differs from the configured migration."""


class UnknownAppliedMigrationError(MigrationError):
    """Raised when a database contains a migration unknown to this application."""


class MigrationIntegrityError(MigrationError):
    """Raised when an integrity check fails."""


class MigrationApplyError(MigrationError):
    """Raised when applying one migration fails."""

    def __init__(self, migration: Migration, cause: BaseException) -> None:
        super().__init__(
            f"migration {migration.version} ({migration.name}) failed: {cause}"
        )
        self.migration = migration
        self.cause = cause


IntegrityHook = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    """One immutable, ordered SQL migration."""

    version: int
    name: str
    statements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise ValueError("migration version must be > 0")
        if not self.name.strip():
            raise ValueError("migration name must be non-empty")
        if any(not statement.strip() for statement in self.statements):
            raise ValueError("migration statements must be non-empty SQL strings")

    @property
    def checksum(self) -> str:
        material = json.dumps(
            {
                "version": self.version,
                "name": self.name,
                "statements": list(self.statements),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class AppliedMigration:
    """Persisted migration metadata."""

    version: int
    name: str
    checksum: str
    applied_at: str
    app_version: str | None


@dataclass(frozen=True)
class MigrationResult:
    """Summary returned after a migration run."""

    database_kind: str
    current_version: int
    applied_versions: tuple[int, ...]


MASTER_MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="M0001_migration_metadata_baseline"),
    Migration(
        version=2,
        name="M0002_master_backbone",
        statements=(
            """
            CREATE TABLE master_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE master_commits (
                commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                commit_id TEXT NOT NULL UNIQUE,
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                reason TEXT,
                run_or_operation_id TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE master_entity_revisions (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_revision INTEGER NOT NULL,
                commit_seq INTEGER NOT NULL REFERENCES master_commits(commit_seq),
                change_kind TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, entity_revision)
            )
            """,
            """
            CREATE INDEX idx_master_entity_revisions_commit_seq
            ON master_entity_revisions(commit_seq)
            """,
        ),
    ),
)

WORK_MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="W0001_migration_metadata_baseline"),
)


def sqlite_integrity_check(connection: sqlite3.Connection) -> None:
    """Run SQLite's full integrity check and raise unless it reports ok."""
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    messages = [str(row[0]) for row in rows]
    if messages != ["ok"]:
        raise MigrationIntegrityError(
            "SQLite integrity_check failed: " + "; ".join(messages)
        )


def _validate_migration_sequence(migrations: Sequence[Migration]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != sorted(versions):
        raise ValueError("migrations must be ordered by ascending version")
    if len(set(versions)) != len(versions):
        raise ValueError("migration versions must be unique")

    names = [migration.name for migration in migrations]
    if len(set(names)) != len(names):
        raise ValueError("migration names must be unique")


def _ensure_schema_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA_MIGRATIONS_SQL)
    connection.commit()


def _read_applied_migrations(
    connection: sqlite3.Connection,
) -> dict[int, AppliedMigration]:
    rows = connection.execute(
        f"""
        SELECT version, name, checksum, applied_at, app_version
        FROM {SCHEMA_MIGRATIONS_TABLE}
        ORDER BY version
        """
    ).fetchall()
    return {
        int(row["version"]): AppliedMigration(
            version=int(row["version"]),
            name=str(row["name"]),
            checksum=str(row["checksum"]),
            applied_at=str(row["applied_at"]),
            app_version=row["app_version"],
        )
        for row in rows
    }


def _validate_applied_migrations(
    configured: Sequence[Migration],
    applied: dict[int, AppliedMigration],
) -> None:
    configured_by_version = {migration.version: migration for migration in configured}

    for version, persisted in applied.items():
        expected = configured_by_version.get(version)
        if expected is None:
            raise UnknownAppliedMigrationError(
                f"database contains unknown applied migration version {version}: "
                f"{persisted.name}"
            )
        if persisted.name != expected.name or persisted.checksum != expected.checksum:
            raise MigrationDriftError(
                f"migration {version} drift detected: "
                f"database has {persisted.name}/{persisted.checksum}, "
                f"application expects {expected.name}/{expected.checksum}"
            )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MigrationRunner:
    """Apply one ordered migration sequence to a SQLite database."""

    def __init__(
        self,
        *,
        database_kind: str,
        migrations: Iterable[Migration],
        app_version: str | None = None,
        pre_integrity_check: IntegrityHook | None = sqlite_integrity_check,
        post_integrity_check: IntegrityHook | None = sqlite_integrity_check,
    ) -> None:
        if not database_kind.strip():
            raise ValueError("database_kind must be non-empty")

        self.database_kind = database_kind
        self.migrations = tuple(migrations)
        self.app_version = app_version
        self.pre_integrity_check = pre_integrity_check
        self.post_integrity_check = post_integrity_check
        _validate_migration_sequence(self.migrations)

    def migrate(self, database_path: str | Path) -> MigrationResult:
        """Migrate a database to the latest configured version."""
        applied_versions: list[int] = []

        with write_connection(database_path) as connection:
            if self.pre_integrity_check is not None:
                self.pre_integrity_check(connection)

            _ensure_schema_migrations(connection)
            applied = _read_applied_migrations(connection)
            _validate_applied_migrations(self.migrations, applied)

            for migration in self.migrations:
                if migration.version in applied:
                    continue

                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        f"""
                        INSERT INTO {SCHEMA_MIGRATIONS_TABLE}
                            (version, name, checksum, applied_at, app_version)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            _utc_now_iso(),
                            self.app_version,
                        ),
                    )
                    connection.commit()
                except Exception as exc:
                    connection.rollback()
                    raise MigrationApplyError(migration, exc) from exc

                applied_versions.append(migration.version)

            if self.post_integrity_check is not None:
                self.post_integrity_check(connection)

            final_applied = _read_applied_migrations(connection)
            current_version = max(final_applied, default=0)

        return MigrationResult(
            database_kind=self.database_kind,
            current_version=current_version,
            applied_versions=tuple(applied_versions),
        )


def migrate_master_database(
    database_path: str | Path,
    *,
    migrations: Iterable[Migration] = MASTER_MIGRATIONS,
    app_version: str | None = None,
    pre_integrity_check: IntegrityHook | None = sqlite_integrity_check,
    post_integrity_check: IntegrityHook | None = sqlite_integrity_check,
) -> MigrationResult:
    """Migrate a Master database using the independent Master sequence."""
    return MigrationRunner(
        database_kind="master",
        migrations=migrations,
        app_version=app_version,
        pre_integrity_check=pre_integrity_check,
        post_integrity_check=post_integrity_check,
    ).migrate(database_path)


def migrate_work_database(
    database_path: str | Path,
    *,
    migrations: Iterable[Migration] = WORK_MIGRATIONS,
    app_version: str | None = None,
    pre_integrity_check: IntegrityHook | None = sqlite_integrity_check,
    post_integrity_check: IntegrityHook | None = sqlite_integrity_check,
) -> MigrationResult:
    """Migrate a Work database using the independent Work sequence."""
    return MigrationRunner(
        database_kind="work",
        migrations=migrations,
        app_version=app_version,
        pre_integrity_check=pre_integrity_check,
        post_integrity_check=post_integrity_check,
    ).migrate(database_path)


__all__ = [
    "MASTER_MIGRATIONS",
    "SCHEMA_MIGRATIONS_TABLE",
    "WORK_MIGRATIONS",
    "AppliedMigration",
    "IntegrityHook",
    "Migration",
    "MigrationApplyError",
    "MigrationDriftError",
    "MigrationError",
    "MigrationIntegrityError",
    "MigrationResult",
    "MigrationRunner",
    "UnknownAppliedMigrationError",
    "migrate_master_database",
    "migrate_work_database",
    "sqlite_integrity_check",
]
