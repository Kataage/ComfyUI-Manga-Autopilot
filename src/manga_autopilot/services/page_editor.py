"""Page + panel editor service (spec sections 14.4, 15.5, 22.5)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from manga_autopilot.models.page import PagePlan, PanelPlan
from manga_autopilot.models.panel import (
    PAGES_FILENAME,
    PANELS_FILENAME,
    PanelLayout,
    PanelRecord,
    load_panel_records,
    write_panel_records,
)
from manga_autopilot.services.page_templates import PageTemplate, get_template

log = logging.getLogger(__name__)


class ProjectNotFoundError(Exception):
    pass


class PageNotFoundError(Exception):
    pass


class PageLayoutError(Exception):
    pass


class LayoutUpdate(BaseModel):
    page_width: int = Field(default=1200, ge=64, le=8192)
    page_height: int = Field(default=1600, ge=64, le=8192)
    panels: list[PanelLayout]

    @field_validator("panels")
    @classmethod
    def _non_overlapping_too_far(cls, value: list[PanelLayout]) -> list[PanelLayout]:
        # Light sanity check: panel ids must be unique.
        ids = [p.panel_id for p in value]
        if len(set(ids)) != len(ids):
            raise ValueError("panel_id values must be unique")
        return value


@dataclass
class PageEditorService:
    """Editor service bound to a single project directory."""

    project_root: Path

    @classmethod
    def for_project(cls, storage_root: Path, project_id: str) -> PageEditorService:
        root = storage_root.expanduser().resolve() / "projects" / project_id
        if not (root / "project.json").exists():
            raise ProjectNotFoundError(f"project not found: {project_id}")
        return cls(project_root=root)

    # -------------------------------------------------------------- helpers
    @property
    def pages_path(self) -> Path:
        return self.project_root / PAGES_FILENAME

    @property
    def panels_path(self) -> Path:
        return self.project_root / PANELS_FILENAME

    def _load_pages(self) -> list[PagePlan]:
        if not self.pages_path.exists():
            return []
        try:
            raw = json.loads(self.pages_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise PageLayoutError(f"could not parse {self.pages_path}: {exc}") from exc
        if not isinstance(raw, list):
            raise PageLayoutError("pages.json must be a JSON array")
        return [PagePlan.model_validate(item) for item in raw]

    def _save_pages(self, pages: list[PagePlan]) -> None:
        payload = [p.model_dump(mode="json") for p in pages]
        self.pages_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # -------------------------------------------------------------- page ops
    def list_pages(self) -> list[PagePlan]:
        return self._load_pages()

    def get_page(self, page_number: int) -> PagePlan:
        for page in self._load_pages():
            if page.page_number == page_number:
                return page
        raise PageNotFoundError(f"page {page_number} not found")

    def upsert_page(self, page: PagePlan) -> PagePlan:
        pages = self._load_pages()
        for i, existing in enumerate(pages):
            if existing.page_number == page.page_number:
                pages[i] = page
                self._save_pages(pages)
                return page
        pages.append(page)
        self._save_pages(pages)
        return page

    def delete_page(self, page_number: int) -> None:
        pages = self._load_pages()
        pages = [p for p in pages if p.page_number != page_number]
        self._save_pages(pages)
        # Drop orphaned panel records.
        panels = [p for p in load_panel_records(self.panels_path) if p.page_number != page_number]
        write_panel_records(self.panels_path, panels)

    # -------------------------------------------------------------- layout
    def get_layout(self, page_number: int) -> list[PanelLayout]:
        return [p.layout for p in load_panel_records(self.panels_path) if p.page_number == page_number and p.layout is not None]

    def apply_template(
        self,
        page_number: int,
        template: PageTemplate | str,
        *,
        panel_prefix: str = "panel",
    ) -> list[PanelLayout]:
        if isinstance(template, str):
            template = get_template(template)
        layouts = template.to_panel_layouts(panel_prefix=panel_prefix)
        # Persist panel records so the page has matching entries.
        panels = load_panel_records(self.panels_path)
        # Remove any previous layouts for this page.
        panels = [p for p in panels if p.page_number != page_number]
        for layout in layouts:
            panels.append(
                PanelRecord(
                    panel_id=layout.panel_id,
                    page_number=page_number,
                    plan=PanelPlan(panel_number=len(panels) + 1),
                    layout=layout,
                )
            )
        write_panel_records(self.panels_path, panels)
        return layouts

    def update_layout(self, page_number: int, update: LayoutUpdate) -> list[PanelLayout]:
        try:
            payload = LayoutUpdate.model_validate(update if isinstance(update, Mapping) else update.model_dump())
        except ValidationError as exc:
            raise PageLayoutError(str(exc)) from exc
        panels = load_panel_records(self.panels_path)
        # Remove existing layouts for this page and replace them.
        panels = [p for p in panels if p.page_number != page_number]
        # Make sure every layout is tied to the requested page.
        for layout in payload.panels:
            panels.append(
                PanelRecord(
                    panel_id=layout.panel_id,
                    page_number=page_number,
                    plan=PanelPlan(panel_number=len(panels) + 1),
                    layout=layout,
                    updated_at=datetime.now(timezone.utc),
                )
            )
        write_panel_records(self.panels_path, panels)
        return list(payload.panels)

    def panel_records(self) -> list[PanelRecord]:
        return load_panel_records(self.panels_path)


__all__ = [
    "ProjectNotFoundError",
    "PageNotFoundError",
    "PageLayoutError",
    "LayoutUpdate",
    "PageEditorService",
]
