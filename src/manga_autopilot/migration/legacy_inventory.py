"""Read-only discovery and inventory of legacy JSON projects.

The legacy topology remains under::

    {storage_root}/projects/{project_id}/

This module never creates, edits, deletes, renames, or normalizes legacy
content. It records what is present so a later importer can make explicit,
reviewable migration decisions.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from manga_autopilot.storage.paths import PROJECTS_SUBDIR, resolve_storage_root

_OPTIONAL_FILES = (
    "story.json",
    "characters.json",
    "pages.json",
    "panels.json",
    "bubbles.json",
    "workflows.json",
    "generation_log.json",
    "qa_report.json",
    "manifest.json",
    "cancel.json",
    "latest_run_id.txt",
)
_OPTIONAL_DIRECTORIES = ("assets", "runs", "jobs", "exports")

_FILE_CATEGORIES = {
    "project.json": "project",
    "story.json": "story",
    "characters.json": "characters",
    "pages.json": "pages",
    "panels.json": "panels",
    "bubbles.json": "bubbles",
    "workflows.json": "workflows",
    "generation_log.json": "generation_log",
    "qa_report.json": "qa",
    "manifest.json": "manifest",
    "cancel.json": "control",
    "latest_run_id.txt": "run_pointer",
}
_DIRECTORY_CATEGORIES = {
    "assets": "assets",
    "runs": "runs",
    "jobs": "jobs",
    "exports": "exports",
}


class LegacyProjectInventoryError(RuntimeError):
    """Base class for legacy inventory failures."""


class LegacyProjectNotFoundError(LegacyProjectInventoryError):
    """Raised when a requested legacy project cannot be inventoried."""


@dataclass(frozen=True)
class LegacyInventoryWarning:
    """One non-destructive import warning."""

    code: str
    message: str
    relative_path: str | None = None
    severity: str = "warning"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyInventoryEntry:
    """One filesystem entry retained in the legacy inventory."""

    relative_path: str
    kind: str
    category: str
    recognized: bool
    size_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyProjectInventory:
    """Structured, read-only report for one legacy project."""

    project_id: str
    root: str
    project_json_id: str | None
    name: str | None
    title: str | None
    entries: tuple[LegacyInventoryEntry, ...]
    missing_optional_files: tuple[str, ...]
    missing_optional_directories: tuple[str, ...]
    warnings: tuple[LegacyInventoryWarning, ...]

    @property
    def unknown_entries(self) -> tuple[LegacyInventoryEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.recognized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "root": self.root,
            "project_json_id": self.project_json_id,
            "name": self.name,
            "title": self.title,
            "entries": [entry.to_dict() for entry in self.entries],
            "missing_optional_files": list(self.missing_optional_files),
            "missing_optional_directories": list(self.missing_optional_directories),
            "warnings": [warning.to_dict() for warning in self.warnings],
            "unknown_entries": [entry.to_dict() for entry in self.unknown_entries],
        }


class LegacyProjectInventoryService:
    """Discover and inventory legacy projects without mutating them."""

    def __init__(self, storage_root: str | Path) -> None:
        self.storage_root = resolve_storage_root(storage_root)
        self.projects_root = self.storage_root / PROJECTS_SUBDIR

    def discover_project_ids(self) -> tuple[str, ...]:
        """Return project dirs with a real, non-symlink project.json."""
        if not self.projects_root.is_dir() or self.projects_root.is_symlink():
            return ()

        project_ids: list[str] = []
        for child in sorted(self.projects_root.iterdir(), key=lambda path: path.name):
            if child.is_symlink() or not child.is_dir():
                continue
            project_json = child / "project.json"
            if project_json.is_symlink():
                continue
            if project_json.is_file():
                project_ids.append(child.name)
        return tuple(project_ids)

    def discover(self) -> tuple[LegacyProjectInventory, ...]:
        """Inventory every discoverable legacy project."""
        return tuple(
            self.inventory_project(project_id)
            for project_id in self.discover_project_ids()
        )

    def inventory_project(self, project_id: str) -> LegacyProjectInventory:
        """Return a structured import inventory for one legacy project."""
        if not project_id:
            raise ValueError("project_id must be non-empty")
        if project_id in {".", ".."} or "/" in project_id or "\\" in project_id:
            raise ValueError("project_id must be a single path component")

        root = self.projects_root / project_id
        project_json = root / "project.json"
        if not root.is_dir() or root.is_symlink():
            raise LegacyProjectNotFoundError(
                f"legacy project is not discoverable: {project_id!r}"
            )
        if not project_json.exists() and not project_json.is_symlink():
            raise LegacyProjectNotFoundError(
                f"legacy project has no project.json: {project_id!r}"
            )

        warnings: list[LegacyInventoryWarning] = []
        project_json_id: str | None = None
        name: str | None = None
        title: str | None = None

        payload = self._read_project_json(project_json, warnings)
        if payload is not None:
            raw_id = payload.get("id")
            if isinstance(raw_id, str) and raw_id:
                project_json_id = raw_id
                if raw_id != project_id:
                    warnings.append(
                        LegacyInventoryWarning(
                            code="PROJECT_ID_MISMATCH",
                            message=(
                                "project.json id does not match the legacy directory name"
                            ),
                            relative_path="project.json",
                        )
                    )
            elif raw_id is not None:
                warnings.append(
                    LegacyInventoryWarning(
                        code="INVALID_PROJECT_ID",
                        message="project.json id is present but is not a non-empty string",
                        relative_path="project.json",
                    )
                )

            raw_name = payload.get("name")
            if isinstance(raw_name, str):
                name = raw_name

            raw_title = payload.get("title")
            if isinstance(raw_title, str):
                title = raw_title

        missing_optional_files = tuple(
            filename
            for filename in _OPTIONAL_FILES
            if not self._is_regular_file(root / filename)
        )
        missing_optional_directories = tuple(
            dirname
            for dirname in _OPTIONAL_DIRECTORIES
            if not self._is_regular_directory(root / dirname)
        )

        for relative_path in missing_optional_files:
            warnings.append(
                LegacyInventoryWarning(
                    code="MISSING_OPTIONAL_FILE",
                    message=f"optional legacy file is absent: {relative_path}",
                    relative_path=relative_path,
                )
            )
        for relative_path in missing_optional_directories:
            warnings.append(
                LegacyInventoryWarning(
                    code="MISSING_OPTIONAL_DIRECTORY",
                    message=f"optional legacy directory is absent: {relative_path}",
                    relative_path=relative_path,
                )
            )

        entries = self._inventory_tree(root, warnings)

        return LegacyProjectInventory(
            project_id=project_id,
            root=str(root.resolve()),
            project_json_id=project_json_id,
            name=name,
            title=title,
            entries=entries,
            missing_optional_files=missing_optional_files,
            missing_optional_directories=missing_optional_directories,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        return path.is_file() and not path.is_symlink()

    @staticmethod
    def _is_regular_directory(path: Path) -> bool:
        return path.is_dir() and not path.is_symlink()

    @staticmethod
    def _read_project_json(
        path: Path,
        warnings: list[LegacyInventoryWarning],
    ) -> dict[str, Any] | None:
        if path.is_symlink():
            warnings.append(
                LegacyInventoryWarning(
                    code="SYMLINK_PROJECT_JSON_IGNORED",
                    message="project.json is a symlink and was not read",
                    relative_path="project.json",
                    severity="error",
                )
            )
            return None
        if not path.is_file():
            warnings.append(
                LegacyInventoryWarning(
                    code="PROJECT_JSON_NOT_REGULAR_FILE",
                    message="project.json is not a regular file",
                    relative_path="project.json",
                    severity="error",
                )
            )
            return None

        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            warnings.append(
                LegacyInventoryWarning(
                    code="PROJECT_JSON_READ_ERROR",
                    message=f"could not read project.json: {exc}",
                    relative_path="project.json",
                    severity="error",
                )
            )
            return None

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            warnings.append(
                LegacyInventoryWarning(
                    code="INVALID_PROJECT_JSON",
                    message=f"project.json is not valid JSON: {exc.msg}",
                    relative_path="project.json",
                    severity="error",
                )
            )
            return None

        if not isinstance(payload, dict):
            warnings.append(
                LegacyInventoryWarning(
                    code="INVALID_PROJECT_JSON_ROOT",
                    message="project.json root must be a JSON object",
                    relative_path="project.json",
                    severity="error",
                )
            )
            return None
        return payload

    @classmethod
    def _inventory_tree(
        cls,
        root: Path,
        warnings: list[LegacyInventoryWarning],
    ) -> tuple[LegacyInventoryEntry, ...]:
        entries: list[LegacyInventoryEntry] = []

        def onerror(exc: OSError) -> None:
            filename = getattr(exc, "filename", None)
            relative_path: str | None = None
            if isinstance(filename, str):
                try:
                    relative_path = Path(filename).relative_to(root).as_posix()
                except ValueError:
                    relative_path = None
            warnings.append(
                LegacyInventoryWarning(
                    code="FILESYSTEM_READ_ERROR",
                    message=f"could not inspect legacy entry: {exc}",
                    relative_path=relative_path,
                    severity="error",
                )
            )

        for current_root, directory_names, file_names in os.walk(
            root,
            topdown=True,
            onerror=onerror,
            followlinks=False,
        ):
            current = Path(current_root)

            for name in sorted(directory_names):
                path = current / name
                relative = path.relative_to(root)
                entry = cls._entry_for(path, relative)
                entries.append(entry)
                if entry.kind == "symlink":
                    warnings.append(
                        LegacyInventoryWarning(
                            code="SYMLINK_ENTRY_IGNORED",
                            message="symlink entry was inventoried but not followed",
                            relative_path=entry.relative_path,
                        )
                    )

            # Explicitly prevent traversal into directory symlinks even though
            # os.walk(followlinks=False) already does so on supported platforms.
            directory_names[:] = [
                name for name in directory_names if not (current / name).is_symlink()
            ]

            for name in sorted(file_names):
                path = current / name
                relative = path.relative_to(root)
                entry = cls._entry_for(path, relative)
                entries.append(entry)
                if entry.kind == "symlink":
                    warnings.append(
                        LegacyInventoryWarning(
                            code="SYMLINK_ENTRY_IGNORED",
                            message="symlink entry was inventoried but not followed",
                            relative_path=entry.relative_path,
                        )
                    )

        return tuple(sorted(entries, key=lambda entry: entry.relative_path))

    @staticmethod
    def _entry_for(path: Path, relative: Path) -> LegacyInventoryEntry:
        relative_path = relative.as_posix()
        parts = relative.parts

        if path.is_symlink():
            kind = "symlink"
            try:
                size_bytes = path.lstat().st_size
            except OSError:
                size_bytes = None
        elif path.is_dir():
            kind = "directory"
            size_bytes = None
        elif path.is_file():
            kind = "file"
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = None
        else:
            kind = "other"
            size_bytes = None

        if kind == "symlink":
            category = "symlink"
            recognized = False
        elif len(parts) == 1 and parts[0] in _FILE_CATEGORIES:
            category = _FILE_CATEGORIES[parts[0]]
            recognized = True
        elif parts and parts[0] in _DIRECTORY_CATEGORIES:
            category = _DIRECTORY_CATEGORIES[parts[0]]
            recognized = True
        else:
            category = "unknown"
            recognized = False

        return LegacyInventoryEntry(
            relative_path=relative_path,
            kind=kind,
            category=category,
            recognized=recognized,
            size_bytes=size_bytes,
        )


__all__ = [
    "LegacyInventoryEntry",
    "LegacyInventoryWarning",
    "LegacyProjectInventory",
    "LegacyProjectInventoryError",
    "LegacyProjectInventoryService",
    "LegacyProjectNotFoundError",
]
