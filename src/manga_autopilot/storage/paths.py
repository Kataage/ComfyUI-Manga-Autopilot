"""Storage path helpers for Manga Autopilot.

Spec reference: ``docs/comfyui_manga_autopilot_spec.md`` section 9.1, 27.1.

The on-disk layout for a single project is::

    {storage_root}/projects/{project_id}/
        project.json
        story.json
        characters.json
        pages.json
        panels.json
        bubbles.json
        workflows.json
        generation_log.json
        qa_report.json
        manifest.json
        assets/
            characters/
            panels/
            pages/
            temp/
        exports/
            pages/
            webtoon/
            pdf/

This module is responsible for creating those directories and computing
canonical paths.  It does not read or write the JSON documents themselves;
that responsibility belongs to the Project Manager service (issue #7).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECTS_SUBDIR = "projects"

ASSET_SUBDIRS: tuple[str, ...] = ("characters", "panels", "pages", "temp")
EXPORT_SUBDIRS: tuple[str, ...] = ("pages", "webtoon", "pdf")


@dataclass(frozen=True)
class ProjectPaths:
    """Resolved on-disk paths for a single project."""

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
        return self.runs / run_id

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


def resolve_storage_root(storage_path: str | Path) -> Path:
    """Return an absolute path for the configured storage root."""
    return Path(storage_path).expanduser().resolve()


def ensure_storage_root(storage_path: str | Path) -> Path:
    """Ensure the storage root and ``projects/`` directory exist."""
    root = resolve_storage_root(storage_path)
    (root / PROJECTS_SUBDIR).mkdir(parents=True, exist_ok=True)
    return root


def project_paths(storage_path: str | Path, project_id: str) -> ProjectPaths:
    """Compute the canonical :class:`ProjectPaths` for a given project id."""
    if not project_id:
        raise ValueError("project_id must be non-empty")
    root = resolve_storage_root(storage_path) / PROJECTS_SUBDIR / project_id
    return ProjectPaths(project_id=project_id, root=root)


def ensure_project_paths(storage_path: str | Path, project_id: str) -> ProjectPaths:
    """Create every directory expected for a project and return its paths."""
    paths = project_paths(storage_path, project_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    for sub in ASSET_SUBDIRS:
        paths.asset(sub).mkdir(parents=True, exist_ok=True)
    for sub in EXPORT_SUBDIRS:
        paths.export(sub).mkdir(parents=True, exist_ok=True)
    return paths


__all__ = [
    "ASSET_SUBDIRS",
    "EXPORT_SUBDIRS",
    "PROJECTS_SUBDIR",
    "ProjectPaths",
    "ensure_project_paths",
    "ensure_storage_root",
    "project_paths",
    "resolve_storage_root",
]
