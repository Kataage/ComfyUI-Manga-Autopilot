"""Project Manager service.

Spec reference: ``docs/comfyui_manga_autopilot_spec.md`` section 6.2.

This service is the single source of truth for ``project.json`` lifecycle:
create, save, load, list, delete.  It does not know about stories, panels or
generation -- those are stitched in by dedicated services on top of it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from manga_autopilot.models.project import CURRENT_PROJECT_SCHEMA_VERSION, Project
from manga_autopilot.services.project_migration import (
    backup_project_document,
    detect_schema_version,
    migrate_document,
    migrate_project_document,
    read_project_document,
    write_project_document,
)
from manga_autopilot.storage.paths import (
    PROJECTS_SUBDIR,
    ProjectPaths,
    ensure_project_paths,
    ensure_storage_root,
    project_paths,
    resolve_storage_root,
)

_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")
_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_\-]")


class ProjectNotFoundError(KeyError):
    """Raised when a requested project does not exist on disk."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(name: str) -> str:
    slug = _SAFE_NAME_PATTERN.sub("_", name.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "project"


def generate_project_id(name: str | None = None) -> str:
    """Generate a stable project id of the form ``proj_{date}_{slug}_{short}``."""

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    slug = _safe_slug(name or "project")
    short = uuid.uuid4().hex[:6]
    return f"proj_{today}_{slug}_{short}"


def validate_project_id(project_id: str) -> None:
    if not project_id or not _PROJECT_ID_PATTERN.match(project_id):
        raise ValueError(
            f"Invalid project_id {project_id!r}: must match {_PROJECT_ID_PATTERN.pattern}"
        )


@dataclass
class ProjectManager:
    """Service object holding the storage root and CRUD operations."""

    storage_path: Path

    def __init__(self, storage_path: str | Path) -> None:
        self.storage_path = resolve_storage_root(storage_path)
        ensure_storage_root(self.storage_path)

    # ------------------------------------------------------------------ utils
    def paths_for(self, project_id: str) -> ProjectPaths:
        validate_project_id(project_id)
        return project_paths(self.storage_path, project_id)

    def projects_dir(self) -> Path:
        return self.storage_path / PROJECTS_SUBDIR

    # ----------------------------------------------------------------- create
    def create(
        self,
        *,
        name: str,
        idea: str | None = None,
        title: str | None = None,
        language: str = "ja",
        project_id: str | None = None,
    ) -> Project:
        pid = project_id or generate_project_id(name)
        validate_project_id(pid)
        paths = ensure_project_paths(self.storage_path, pid)
        if paths.project_json.exists():
            raise FileExistsError(f"Project already exists: {pid}")
        project = Project(
            id=pid,
            name=name,
            title=title,
            idea=idea,
            language=language,
        )
        self._write(project)
        return project

    # ------------------------------------------------------------------- save
    def save(self, project: Project) -> Project:
        validate_project_id(project.id)
        ensure_project_paths(self.storage_path, project.id)
        project.updated_at = _utc_now_iso()
        self._write(project)
        return project

    def _write(self, project: Project) -> None:
        """Persist ``project`` without discarding fields this build does not model.

        The on-disk document stays the authority for ``schema_version`` and
        ``migration_history``: a caller holding a stale in-memory Project cannot
        erase the migration audit trail by saving.
        """
        paths = project_paths(self.storage_path, project.id)
        target = paths.project_json

        document: dict = {}
        if target.exists():
            existing = read_project_document(target)
            if detect_schema_version(existing) < CURRENT_PROJECT_SCHEMA_VERSION:
                backup_project_document(target)
            document = migrate_document(existing).document

        history = document.get("migration_history", [])
        payload = project.model_dump(mode="json")
        payload.pop("migration_history", None)
        payload.pop("schema_version", None)

        document.update(payload)
        document["schema_version"] = CURRENT_PROJECT_SCHEMA_VERSION
        document["migration_history"] = history
        write_project_document(target, document)

    # ------------------------------------------------------------------- load
    def load(self, project_id: str) -> Project:
        validate_project_id(project_id)
        paths = project_paths(self.storage_path, project_id)
        if not paths.project_json.exists():
            raise ProjectNotFoundError(project_id)
        result = migrate_project_document(paths.project_json)
        return Project.model_validate(result.document)

    def exists(self, project_id: str) -> bool:
        paths = project_paths(self.storage_path, project_id)
        return paths.project_json.exists()

    # ------------------------------------------------------------------- list
    def list_ids(self) -> list[str]:
        projects_dir = self.projects_dir()
        if not projects_dir.exists():
            return []
        ids: list[str] = []
        for child in sorted(projects_dir.iterdir()):
            if child.is_dir() and (child / "project.json").exists():
                ids.append(child.name)
        return ids

    def list_all(self) -> Iterable[Project]:
        for pid in self.list_ids():
            yield self.load(pid)

    # ---------------------------------------------------------------- delete
    def delete(self, project_id: str) -> None:
        validate_project_id(project_id)
        paths = project_paths(self.storage_path, project_id)
        if not paths.root.exists():
            raise ProjectNotFoundError(project_id)
        _rmtree(paths.root)


def _rmtree(path: Path) -> None:
    """Recursively remove ``path`` without importing shutil at module load."""

    import shutil

    shutil.rmtree(path)


__all__ = [
    "ProjectManager",
    "ProjectNotFoundError",
    "generate_project_id",
    "validate_project_id",
]
