"""Storage path helpers for Manga Autopilot.

The v2 storage topology is::

    {storage_root}/
        master.sqlite3
        shared_assets/
        works/
            {work_id}/
                manifest.json
                work.sqlite3
                assets/
                cache/
                exports/

Legacy JSON projects remain readable during migration under::

    {storage_root}/projects/{project_id}/

This module only resolves and creates filesystem paths. It does not create any
SQLite schema or migrate legacy project data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MASTER_DB_FILENAME = "master.sqlite3"
SHARED_ASSETS_SUBDIR = "shared_assets"
WORKS_SUBDIR = "works"

# Legacy v1 JSON project topology. Keep these names available until migration
# callers have moved to explicit legacy helpers.
PROJECTS_SUBDIR = "projects"

WORK_DB_FILENAME = "work.sqlite3"
WORK_MANIFEST_FILENAME = "manifest.json"

ASSET_SUBDIRS: tuple[str, ...] = ("characters", "panels", "pages", "temp")
EXPORT_SUBDIRS: tuple[str, ...] = ("pages", "webtoon", "pdf")


def _safe_path_component(value: str, *, field_name: str) -> str:
    """Validate a user-controlled ID before using it as one path component."""
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    if value in {".", ".."}:
        raise ValueError(f"{field_name} must not be a relative path segment")
    if "/" in value or "\\" in value:
        raise ValueError(f"{field_name} must be a single path component")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if Path(value).is_absolute():
        raise ValueError(f"{field_name} must not be absolute")
    return value


@dataclass(frozen=True)
class StoragePaths:
    """Resolved top-level v2 storage paths."""

    root: Path

    @property
    def master_db(self) -> Path:
        return self.root / MASTER_DB_FILENAME

    @property
    def shared_assets(self) -> Path:
        return self.root / SHARED_ASSETS_SUBDIR

    @property
    def works(self) -> Path:
        return self.root / WORKS_SUBDIR

    @property
    def legacy_projects(self) -> Path:
        return self.root / PROJECTS_SUBDIR


@dataclass(frozen=True)
class WorkPaths:
    """Resolved on-disk paths for one v2 Work."""

    work_id: str
    root: Path

    @property
    def manifest_json(self) -> Path:
        return self.root / WORK_MANIFEST_FILENAME

    @property
    def work_db(self) -> Path:
        return self.root / WORK_DB_FILENAME

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def exports(self) -> Path:
        return self.root / "exports"


@dataclass(frozen=True)
class ProjectPaths:
    """Resolved paths for one legacy JSON project."""

    project_id: str
    root: Path

    @property
    def project_json(self) -> Path:
        return self.root / "project.json"

    @property
    def story_json(self) -> Path:
        return self.root / "story.json"

    @property
    def characters_json(self) -> Path:
        return self.root / "characters.json"

    @property
    def pages_json(self) -> Path:
        return self.root / "pages.json"

    @property
    def panels_json(self) -> Path:
        return self.root / "panels.json"

    @property
    def bubbles_json(self) -> Path:
        return self.root / "bubbles.json"

    @property
    def workflows_json(self) -> Path:
        return self.root / "workflows.json"

    @property
    def generation_log_json(self) -> Path:
        return self.root / "generation_log.json"

    @property
    def qa_report_json(self) -> Path:
        return self.root / "qa_report.json"

    @property
    def manifest_json(self) -> Path:
        return self.root / "manifest.json"

    @property
    def cancel_json(self) -> Path:
        return self.root / "cancel.json"

    @property
    def latest_run_id_txt(self) -> Path:
        return self.root / "latest_run_id.txt"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    def run_dir(self, run_id: str) -> Path:
        safe_run_id = _safe_path_component(run_id, field_name="run_id")
        return self.runs / safe_run_id

    def run_json(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def run_generation_log_json(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "generation_log.json"

    def run_manifest_json(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "manifest.json"

    def run_panels_json(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "panels.json"

    def run_bubbles_json(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "bubbles.json"

    def run_pages_json(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "pages.json"

    def run_assets_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "assets"

    def run_panel_assets_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "assets" / "panels"

    def run_jobs_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "jobs"

    def run_exports_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "exports"

    def run_exports_pages_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "exports" / "pages"

    def run_exports_webtoon_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "exports" / "webtoon"

    def run_exports_pdf_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "exports" / "pdf"

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    def asset(self, name: str) -> Path:
        if name not in ASSET_SUBDIRS:
            raise ValueError(f"Unknown asset subdir: {name!r}; expected one of {ASSET_SUBDIRS}")
        return self.assets / name

    def export(self, name: str) -> Path:
        if name not in EXPORT_SUBDIRS:
            raise ValueError(f"Unknown export subdir: {name!r}; expected one of {EXPORT_SUBDIRS}")
        return self.exports / name


# Explicit compatibility name for migration code. Existing callers may keep
# importing ProjectPaths until they are migrated.
LegacyProjectPaths = ProjectPaths


def resolve_storage_root(storage_path: str | Path) -> Path:
    """Return an absolute path for the configured storage root."""
    return Path(storage_path).expanduser().resolve()


def storage_paths(storage_path: str | Path) -> StoragePaths:
    """Resolve the canonical top-level v2 storage paths without creating them."""
    return StoragePaths(root=resolve_storage_root(storage_path))


def ensure_storage_root(storage_path: str | Path) -> Path:
    """Ensure both v2 and legacy top-level storage directories exist."""
    paths = storage_paths(storage_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.shared_assets.mkdir(parents=True, exist_ok=True)
    paths.works.mkdir(parents=True, exist_ok=True)
    paths.legacy_projects.mkdir(parents=True, exist_ok=True)
    return paths.root


def work_paths(storage_path: str | Path, work_id: str) -> WorkPaths:
    """Resolve the canonical paths for one v2 Work."""
    safe_work_id = _safe_path_component(work_id, field_name="work_id")
    root = storage_paths(storage_path).works / safe_work_id
    return WorkPaths(work_id=safe_work_id, root=root)


def ensure_work_paths(storage_path: str | Path, work_id: str) -> WorkPaths:
    """Create the directory skeleton for one v2 Work without creating its DB."""
    ensure_storage_root(storage_path)
    paths = work_paths(storage_path, work_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.assets.mkdir(parents=True, exist_ok=True)
    paths.cache.mkdir(parents=True, exist_ok=True)
    paths.exports.mkdir(parents=True, exist_ok=True)
    return paths


def legacy_project_paths(storage_path: str | Path, project_id: str) -> ProjectPaths:
    """Resolve paths for a legacy JSON project."""
    safe_project_id = _safe_path_component(project_id, field_name="project_id")
    root = storage_paths(storage_path).legacy_projects / safe_project_id
    return ProjectPaths(project_id=safe_project_id, root=root)


def ensure_legacy_project_paths(storage_path: str | Path, project_id: str) -> ProjectPaths:
    """Create every directory expected for a legacy JSON project."""
    ensure_storage_root(storage_path)
    paths = legacy_project_paths(storage_path, project_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    for sub in ASSET_SUBDIRS:
        paths.asset(sub).mkdir(parents=True, exist_ok=True)
    for sub in EXPORT_SUBDIRS:
        paths.export(sub).mkdir(parents=True, exist_ok=True)
    return paths


def project_paths(storage_path: str | Path, project_id: str) -> ProjectPaths:
    """Compatibility wrapper for :func:`legacy_project_paths`."""
    return legacy_project_paths(storage_path, project_id)


def ensure_project_paths(storage_path: str | Path, project_id: str) -> ProjectPaths:
    """Compatibility wrapper for :func:`ensure_legacy_project_paths`."""
    return ensure_legacy_project_paths(storage_path, project_id)


__all__ = [
    "ASSET_SUBDIRS",
    "EXPORT_SUBDIRS",
    "MASTER_DB_FILENAME",
    "PROJECTS_SUBDIR",
    "SHARED_ASSETS_SUBDIR",
    "WORKS_SUBDIR",
    "WORK_DB_FILENAME",
    "WORK_MANIFEST_FILENAME",
    "LegacyProjectPaths",
    "ProjectPaths",
    "StoragePaths",
    "WorkPaths",
    "ensure_legacy_project_paths",
    "ensure_project_paths",
    "ensure_storage_root",
    "ensure_work_paths",
    "legacy_project_paths",
    "project_paths",
    "resolve_storage_root",
    "storage_paths",
    "work_paths",
]
