"""Lazy, versioned migration of ``project.json``.

Reading an old document never touches the disk: `migrate_project_document`
returns the upgraded document in memory. The file is only rewritten when the
caller saves, and the Project Manager takes a byte-identical timestamped backup
under ``backups/`` before that first migrated write.

Writes go through a temporary sibling file and `Path.replace`, so an interrupted
save leaves the original document intact rather than a truncated one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manga_autopilot.models.project import CURRENT_PROJECT_SCHEMA_VERSION

log = logging.getLogger(__name__)

#: Documents written before the schema was versioned.
LEGACY_SCHEMA_VERSION = 1

BACKUP_DIR_NAME = "backups"


class UnsupportedSchemaVersionError(ValueError):
    """Raised when a document was written by a newer build than this one."""


class ProjectDocumentError(ValueError):
    """Raised when a project document cannot be read as a JSON object."""


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of migrating one document in memory."""

    path: Path
    from_version: int
    to_version: int
    migrated: bool
    document: dict[str, Any]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_schema_version(document: dict[str, Any]) -> int:
    """Return the schema version of `document`, treating an absent field as 1."""
    raw = document.get("schema_version", LEGACY_SCHEMA_VERSION)
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise ProjectDocumentError(f"schema_version is not an integer: {raw!r}") from exc
    if version < LEGACY_SCHEMA_VERSION:
        raise ProjectDocumentError(f"schema_version must be >= 1, got {version}")
    return version


def read_project_document(path: Path) -> dict[str, Any]:
    """Read a project document as a plain dict."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ProjectDocumentError(f"{path} does not contain a JSON object")
    return document


def _migrate_1_to_2(document: dict[str, Any]) -> dict[str, Any]:
    """Stamp the schema version. Every other field, asset paths included, is left alone."""
    return dict(document)


_MIGRATIONS = {1: _migrate_1_to_2}


def migrate_document(document: dict[str, Any]) -> MigrationResult:
    """Upgrade `document` to the current schema version without touching disk."""
    from_version = detect_schema_version(document)
    if from_version > CURRENT_PROJECT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"project.json schema version {from_version} is newer than the supported "
            f"version {CURRENT_PROJECT_SCHEMA_VERSION}; upgrade the extension"
        )

    upgraded = dict(document)
    history = list(upgraded.get("migration_history") or [])
    version = from_version
    while version < CURRENT_PROJECT_SCHEMA_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:
            raise UnsupportedSchemaVersionError(
                f"no migration registered from schema version {version}"
            )
        upgraded = step(upgraded)
        history.append(
            {
                "from_version": version,
                "to_version": version + 1,
                "migrated_at": _utc_now_iso(),
            }
        )
        version += 1

    upgraded["schema_version"] = CURRENT_PROJECT_SCHEMA_VERSION
    upgraded["migration_history"] = history
    return MigrationResult(
        path=Path(),
        from_version=from_version,
        to_version=CURRENT_PROJECT_SCHEMA_VERSION,
        migrated=from_version != CURRENT_PROJECT_SCHEMA_VERSION,
        document=upgraded,
    )


def migrate_project_document(path: Path) -> MigrationResult:
    """Read `path` and return its migrated document. The file is not modified."""
    path = Path(path)
    result = migrate_document(read_project_document(path))
    if result.migrated:
        log.info(
            "migrated %s from schema version %d to %d (in memory)",
            path.name,
            result.from_version,
            result.to_version,
        )
    return MigrationResult(
        path=path,
        from_version=result.from_version,
        to_version=result.to_version,
        migrated=result.migrated,
        document=result.document,
    )


def backup_project_document(path: Path) -> Path:
    """Copy `path` byte-for-byte into ``backups/`` under a timestamped name."""
    path = Path(path)
    payload = path.read_bytes()
    backup_dir = path.parent / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    target = backup_dir / f"{path.name}.{stamp}.bak"
    suffix = 1
    while target.exists():
        target = backup_dir / f"{path.name}.{stamp}-{suffix}.bak"
        suffix += 1

    target.write_bytes(payload)
    log.info("backed up %s to %s", path.name, target.name)
    return target


def write_project_document(path: Path, document: dict[str, Any]) -> None:
    """Serialise `document` to `path` via a temporary sibling and an atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(payload, encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


__all__ = [
    "BACKUP_DIR_NAME",
    "LEGACY_SCHEMA_VERSION",
    "MigrationResult",
    "ProjectDocumentError",
    "UnsupportedSchemaVersionError",
    "backup_project_document",
    "detect_schema_version",
    "migrate_document",
    "migrate_project_document",
    "read_project_document",
    "write_project_document",
]
