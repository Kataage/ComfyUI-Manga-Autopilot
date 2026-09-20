"""Versioned SQLite migration runner for Master and Work databases.

Migration safety follows the approved v2 contract:

checkpoint WAL -> validate DB identity/history -> integrity_check -> verified
backup -> transactional migration -> post-migration validation -> record
migration versions -> commit.

Fresh databases do not need a backup. Existing recognized databases are backed
up only when at least one migration is pending.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manga_autopilot.storage.paths import UnsafeStoragePathError
from manga_autopilot.storage.sqlite import read_connection, write_connection

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


class UnrecognizedDatabaseError(MigrationError):
    """Raised before mutation when an existing DB is not a recognized app DB."""


class DatabaseIdentityMismatchError(MigrationError):
    """Raised when an existing DB belongs to a different database kind."""


class MigrationIntegrityError(MigrationError):
    """Raised when an integrity or constraint check fails."""


class MigrationBatchError(MigrationError):
    """Base error carrying structured pending-migration attribution."""

    def __init__(
        self,
        message: str,
        *,
        pending_migrations: Sequence[Migration],
        cause: BaseException | None = None,
    ) -> None:
        self.pending_migrations = tuple(pending_migrations)
        self.pending_versions = tuple(
            migration.version for migration in self.pending_migrations
        )
        self.target_version = max(self.pending_versions, default=None)
        self.cause = cause
        pending_ids = ", ".join(
            f"{migration.version}:{migration.name}"
            for migration in self.pending_migrations
        ) or "none"
        suffix = f"; pending migrations [{pending_ids}]"
        if self.target_version is not None:
            suffix += f"; target version {self.target_version}"
        if cause is not None:
            suffix += f": {cause}"
        super().__init__(message + suffix)


class MigrationBackupError(MigrationBatchError):
    """Raised when a pre-migration backup cannot be created or verified."""


class MigrationValidationError(MigrationBatchError):
    """Raised when post-migration validation fails before commit."""


class MigrationApplyError(MigrationError):
    """Raised when applying one migration fails."""

    def __init__(self, migration: Migration, cause: BaseException) -> None:
        super().__init__(
            f"migration {migration.version} ({migration.name}) failed: {cause}"
        )
        self.migration = migration
        self.cause = cause


IntegrityHook = Callable[[sqlite3.Connection], None]
InitializationHook = Callable[[sqlite3.Connection], None]


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
    backup_path: Path | None = None


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
    Migration(
        version=3,
        name="M0003_work_catalog",
        statements=(
            """
            CREATE TABLE work_catalog (
                work_id TEXT PRIMARY KEY,
                universe_id TEXT,
                series_id TEXT,
                title TEXT NOT NULL,
                work_kind TEXT NOT NULL,
                relative_work_path TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                manifest_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_opened_at TEXT
            )
            """,
            """
            CREATE INDEX idx_work_catalog_series_id
            ON work_catalog(series_id)
            """,
            """
            CREATE INDEX idx_work_catalog_status
            ON work_catalog(status)
            """,
        ),
    ),
    Migration(
        version=4,
        name="M0004_canonical_database_kind",
        statements=(
            """
            UPDATE master_metadata
            SET value = 'manga_autopilot_master'
            WHERE key = 'database_kind' AND value = 'master'
            """,
        ),
    ),
)

WORK_MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="W0001_migration_metadata_baseline"),
    Migration(
        version=2,
        name="W0002_work_backbone",
        statements=(
            """
            CREATE TABLE work_database_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE commits (
                commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                commit_id TEXT NOT NULL UNIQUE,
                parent_commit_seq INTEGER,
                run_id TEXT,
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                operation_type TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE entity_revisions (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_revision INTEGER NOT NULL,
                commit_seq INTEGER NOT NULL REFERENCES commits(commit_seq),
                change_kind TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, entity_revision)
            )
            """,
            """
            CREATE INDEX idx_entity_revisions_commit_seq
            ON entity_revisions(commit_seq)
            """,
            """
            CREATE TABLE work_metadata (
                work_id TEXT PRIMARY KEY,
                universe_source_id TEXT,
                series_source_id TEXT,
                source_checkpoint_id TEXT,
                title TEXT NOT NULL,
                work_kind TEXT NOT NULL,
                language TEXT NOT NULL,
                reading_direction TEXT NOT NULL,
                status TEXT NOT NULL,
                current_commit_seq INTEGER NOT NULL,
                current_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """,
        ),
    ),
)


def sqlite_integrity_check(connection: sqlite3.Connection) -> None:
    """Run SQLite's full integrity check and raise unless it reports ok."""
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    messages = [str(row[0]) for row in rows]
    if messages != ["ok"]:
        raise MigrationIntegrityError(
            "SQLite integrity_check failed: " + "; ".join(messages)
        )


def sqlite_constraint_check(connection: sqlite3.Connection) -> None:
    """Reject unresolved SQLite foreign-key violations."""
    rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if rows:
        details = "; ".join(str(tuple(row)) for row in rows[:20])
        raise MigrationIntegrityError(
            "SQLite foreign_key_check failed: " + details
        )


def sqlite_post_migration_check(connection: sqlite3.Connection) -> None:
    """Validate integrity and constraints before migration rows are recorded."""
    sqlite_integrity_check(connection)
    sqlite_constraint_check(connection)


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

    ordered_applied = [applied[version] for version in sorted(applied)]
    for persisted in ordered_applied:
        expected = configured_by_version.get(persisted.version)
        if expected is None:
            raise UnknownAppliedMigrationError(
                "database contains unknown applied migration version "
                f"{persisted.version}: {persisted.name}"
            )
        if persisted.name != expected.name or persisted.checksum != expected.checksum:
            raise MigrationDriftError(
                f"migration {persisted.version} drift detected: "
                f"database has {persisted.name}/{persisted.checksum}, "
                f"application expects {expected.name}/{expected.checksum}"
            )

    expected_prefix = tuple(
        migration.version for migration in configured[: len(ordered_applied)]
    )
    actual_versions = tuple(migration.version for migration in ordered_applied)
    if actual_versions != expected_prefix:
        raise MigrationDriftError(
            "applied migration history is not an ordered configured prefix: "
            f"database has versions {actual_versions}, expected prefix "
            f"{expected_prefix}"
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migration_database_path(database_path: str | Path) -> Path:
    raw = Path(database_path).expanduser().absolute()
    if raw.is_symlink():
        raise UnsafeStoragePathError(
            f"migration database must not be a symlink: {raw}"
        )
    return raw.resolve()


def _existing_nonempty_database(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _cleanup_failed_fresh_database(path: Path) -> None:
    """Best-effort cleanup for a DB file created by this failed fresh attempt."""
    for candidate in (
        path.with_name(path.name + "-shm"),
        path.with_name(path.name + "-wal"),
        path,
    ):
        try:
            if candidate.is_symlink():
                continue
            if candidate.is_file():
                candidate.unlink()
        except OSError:
            # Cleanup must never mask the primary migration failure.
            pass


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


class MigrationRunner:
    """Apply one ordered migration sequence to a SQLite database safely."""

    def __init__(
        self,
        *,
        database_kind: str,
        migrations: Iterable[Migration],
        app_version: str | None = None,
        pre_integrity_check: IntegrityHook | None = sqlite_integrity_check,
        post_integrity_check: IntegrityHook | None = sqlite_post_migration_check,
        identity_table: str | None = None,
        identity_key: str = "database_kind",
        accepted_database_kinds: Iterable[str] = (),
        required_identity_keys: Iterable[str] = (),
        expected_identity_values: Mapping[str, str] | None = None,
        identity_migration_version: int | None = None,
        fresh_initializer: InitializationHook | None = None,
    ) -> None:
        if not database_kind.strip():
            raise ValueError("database_kind must be non-empty")

        self.database_kind = database_kind
        self.migrations = tuple(migrations)
        self.app_version = app_version
        self.pre_integrity_check = pre_integrity_check
        self.post_integrity_check = post_integrity_check
        self.identity_table = identity_table
        self.identity_key = identity_key
        self.accepted_database_kinds = frozenset(accepted_database_kinds)
        self.required_identity_keys = tuple(required_identity_keys)
        self.expected_identity_values = dict(expected_identity_values or {})
        self.identity_migration_version = identity_migration_version
        self.fresh_initializer = fresh_initializer
        _validate_migration_sequence(self.migrations)

    def migrate(self, database_path: str | Path) -> MigrationResult:
        """Migrate a database using backup-first, all-or-nothing semantics."""
        path = _migration_database_path(database_path)
        path_existed_before = path.exists()
        existed = _existing_nonempty_database(path)

        applied = self._inspect_existing_database(path) if existed else {}
        pending = tuple(
            migration
            for migration in self.migrations
            if migration.version not in applied
        )

        if not pending:
            return MigrationResult(
                database_kind=self.database_kind,
                current_version=max(applied, default=0),
                applied_versions=(),
                backup_path=None,
            )

        backup_path: Path | None = None
        if existed:
            backup_path = self._prepare_verified_backup(
                path,
                current_version=max(applied, default=0),
                pending_migrations=pending,
            )

        try:
            with write_connection(path) as connection:
                if self.pre_integrity_check is not None and not existed:
                    self.pre_integrity_check(connection)

                try:
                    connection.execute("BEGIN IMMEDIATE")
                    _ensure_schema_migrations(connection)

                    for migration in pending:
                        try:
                            for statement in migration.statements:
                                connection.execute(statement)
                        except Exception as exc:
                            raise MigrationApplyError(migration, exc) from exc

                    if not existed and self.fresh_initializer is not None:
                        self.fresh_initializer(connection)

                    if self.post_integrity_check is not None:
                        try:
                            self.post_integrity_check(connection)
                        except Exception as exc:
                            raise MigrationValidationError(
                                "post-migration validation failed before migration "
                                "records were committed",
                                pending_migrations=pending,
                                cause=exc,
                            ) from exc

                    applied_at = _utc_now_iso()
                    for migration in pending:
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
                                applied_at,
                                self.app_version,
                            ),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

                final_applied = _read_applied_migrations(connection)
                current_version = max(final_applied, default=0)
        except Exception:
            if not path_existed_before:
                _cleanup_failed_fresh_database(path)
            raise

        return MigrationResult(
            database_kind=self.database_kind,
            current_version=current_version,
            applied_versions=tuple(m.version for m in pending),
            backup_path=backup_path,
        )

    def _inspect_existing_database(
        self,
        path: Path,
    ) -> dict[int, AppliedMigration]:
        try:
            with read_connection(path) as connection:
                tables = _table_names(connection)
                if SCHEMA_MIGRATIONS_TABLE not in tables:
                    raise UnrecognizedDatabaseError(
                        "existing non-empty SQLite database has no recognized "
                        "Manga Autopilot migration history"
                    )

                try:
                    applied = _read_applied_migrations(connection)
                except sqlite3.DatabaseError as exc:
                    raise UnrecognizedDatabaseError(
                        "schema_migrations exists but is not readable as the "
                        "Manga Autopilot migration table"
                    ) from exc

                _validate_applied_migrations(self.migrations, applied)
                self._validate_identity(connection, tables, applied)
                return applied
        except sqlite3.DatabaseError as exc:
            raise UnrecognizedDatabaseError(
                f"existing database is not a readable SQLite database: {path}"
            ) from exc

    def _validate_identity(
        self,
        connection: sqlite3.Connection,
        tables: set[str],
        applied: dict[int, AppliedMigration],
    ) -> None:
        if self.identity_table is None:
            return

        if self.identity_table not in tables:
            latest = max(applied, default=0)
            if (
                self.identity_migration_version is not None
                and latest < self.identity_migration_version
            ):
                return
            raise UnrecognizedDatabaseError(
                f"recognized migration history is missing identity table "
                f"{self.identity_table!r}"
            )

        row = connection.execute(
            f"SELECT value FROM {self.identity_table} WHERE key = ?",
            (self.identity_key,),
        ).fetchone()
        if row is None:
            raise UnrecognizedDatabaseError(
                f"identity table {self.identity_table!r} has no "
                f"{self.identity_key!r} value"
            )

        actual_kind = str(row[0])
        if actual_kind not in self.accepted_database_kinds:
            expected = ", ".join(sorted(self.accepted_database_kinds))
            raise DatabaseIdentityMismatchError(
                f"database identity mismatch for {self.database_kind}: "
                f"got {actual_kind!r}; accepted values: {expected}"
            )

        if self.required_identity_keys:
            placeholders = ", ".join("?" for _ in self.required_identity_keys)
            rows = connection.execute(
                f"SELECT key, value FROM {self.identity_table} "
                f"WHERE key IN ({placeholders})",
                self.required_identity_keys,
            ).fetchall()
            metadata = {
                str(metadata_row["key"]): str(metadata_row["value"])
                for metadata_row in rows
            }
            missing = [
                key
                for key in self.required_identity_keys
                if not metadata.get(key)
            ]
            if missing:
                raise UnrecognizedDatabaseError(
                    f"recognized {self.database_kind} database has incomplete "
                    "identity metadata: "
                    + ", ".join(sorted(missing))
                )

            for key, expected_value in self.expected_identity_values.items():
                actual_value = metadata.get(key)
                if actual_value != expected_value:
                    raise DatabaseIdentityMismatchError(
                        f"database identity mismatch for {self.database_kind} "
                        f"{key}: got {actual_value!r}, expected "
                        f"{expected_value!r}"
                    )

    def _prepare_verified_backup(
        self,
        path: Path,
        *,
        current_version: int,
        pending_migrations: Sequence[Migration],
    ) -> Path:
        target_version = max(
            migration.version for migration in pending_migrations
        )
        backup = path.with_name(
            f"{path.name}.backup-v{current_version}-to-v{target_version}"
        )
        temp = backup.with_name(backup.name + ".tmp")
        if backup.is_symlink():
            raise MigrationBackupError(
                f"migration backup path must not be a symlink: {backup}",
                pending_migrations=pending_migrations,
            )
        if temp.is_symlink():
            raise MigrationBackupError(
                f"migration backup temp path must not be a symlink: {temp}",
                pending_migrations=pending_migrations,
            )
        if temp.exists():
            temp.unlink()

        try:
            with write_connection(path) as source:
                checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and int(checkpoint[0]) != 0:
                    raise MigrationBackupError(
                        f"WAL checkpoint was busy for {path}: {tuple(checkpoint)}",
                        pending_migrations=pending_migrations,
                    )

                self._validate_identity(
                    source,
                    _table_names(source),
                    _read_applied_migrations(source),
                )
                if self.pre_integrity_check is not None:
                    self.pre_integrity_check(source)

                destination = sqlite3.connect(temp)
                try:
                    source.backup(destination)
                finally:
                    destination.close()

            with read_connection(temp) as verification:
                sqlite_integrity_check(verification)
                self._validate_identity(
                    verification,
                    _table_names(verification),
                    _read_applied_migrations(verification),
                )

            os.replace(temp, backup)
            return backup
        except MigrationError:
            if temp.exists():
                temp.unlink()
            raise
        except Exception as exc:
            if temp.exists():
                temp.unlink()
            raise MigrationBackupError(
                f"failed to create verified backup for {path}",
                pending_migrations=pending_migrations,
                cause=exc,
            ) from exc


def _new_database_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def migrate_master_database(
    database_path: str | Path,
    *,
    migrations: Iterable[Migration] = MASTER_MIGRATIONS,
    app_version: str | None = None,
    database_id: str | None = None,
    created_at: str | None = None,
    pre_integrity_check: IntegrityHook | None = sqlite_integrity_check,
    post_integrity_check: IntegrityHook | None = sqlite_post_migration_check,
) -> MigrationResult:
    """Migrate a Master database and atomically initialize fresh DB identity."""
    path = _migration_database_path(database_path)
    fresh = not _existing_nonempty_database(path)
    initial_database_id = database_id or _new_database_id("master")
    initial_created_at = created_at or _utc_now_iso()

    def initialize_identity(connection: sqlite3.Connection) -> None:
        connection.executemany(
            """
            INSERT INTO master_metadata (key, value)
            VALUES (?, ?)
            """,
            (
                ("database_kind", "manga_autopilot_master"),
                ("database_id", initial_database_id),
                ("format_version", "2"),
                ("created_at", initial_created_at),
            ),
        )

    return MigrationRunner(
        database_kind="master",
        migrations=migrations,
        app_version=app_version,
        pre_integrity_check=pre_integrity_check,
        post_integrity_check=post_integrity_check,
        identity_table="master_metadata",
        accepted_database_kinds=("master", "manga_autopilot_master"),
        required_identity_keys=(
            "database_kind",
            "database_id",
            "format_version",
            "created_at",
        ),
        expected_identity_values={"format_version": "2"},
        identity_migration_version=2,
        fresh_initializer=initialize_identity if fresh else None,
    ).migrate(path)


def migrate_work_database(
    database_path: str | Path,
    *,
    migrations: Iterable[Migration] = WORK_MIGRATIONS,
    app_version: str | None = None,
    work_id: str | None = None,
    database_id: str | None = None,
    created_at: str | None = None,
    pre_integrity_check: IntegrityHook | None = sqlite_integrity_check,
    post_integrity_check: IntegrityHook | None = sqlite_post_migration_check,
) -> MigrationResult:
    """Migrate a Work DB and atomically initialize identity when it is fresh."""
    path = _migration_database_path(database_path)
    fresh = not _existing_nonempty_database(path)
    if fresh and (work_id is None or not work_id.strip()):
        raise ValueError("work_id is required when migrating a fresh Work database")

    initial_database_id = database_id or _new_database_id("workdb")
    initial_created_at = created_at or _utc_now_iso()

    def initialize_identity(connection: sqlite3.Connection) -> None:
        assert work_id is not None
        connection.executemany(
            """
            INSERT INTO work_database_metadata (key, value)
            VALUES (?, ?)
            """,
            (
                ("database_kind", "work"),
                ("database_id", initial_database_id),
                ("format_version", "2"),
                ("work_id", work_id),
                ("created_at", initial_created_at),
            ),
        )

    return MigrationRunner(
        database_kind="work",
        migrations=migrations,
        app_version=app_version,
        pre_integrity_check=pre_integrity_check,
        post_integrity_check=post_integrity_check,
        identity_table="work_database_metadata",
        accepted_database_kinds=("work", "manga_autopilot_work"),
        required_identity_keys=(
            "database_kind",
            "database_id",
            "format_version",
            "work_id",
            "created_at",
        ),
        expected_identity_values={
            "format_version": "2",
            **({"work_id": work_id} if work_id is not None else {}),
        },
        identity_migration_version=2,
        fresh_initializer=initialize_identity if fresh else None,
    ).migrate(path)


__all__ = [
    "MASTER_MIGRATIONS",
    "SCHEMA_MIGRATIONS_TABLE",
    "WORK_MIGRATIONS",
    "AppliedMigration",
    "DatabaseIdentityMismatchError",
    "InitializationHook",
    "IntegrityHook",
    "Migration",
    "MigrationApplyError",
    "MigrationBackupError",
    "MigrationBatchError",
    "MigrationDriftError",
    "MigrationError",
    "MigrationIntegrityError",
    "MigrationResult",
    "MigrationRunner",
    "MigrationValidationError",
    "UnknownAppliedMigrationError",
    "UnrecognizedDatabaseError",
    "migrate_master_database",
    "migrate_work_database",
    "sqlite_constraint_check",
    "sqlite_integrity_check",
    "sqlite_post_migration_check",
]
