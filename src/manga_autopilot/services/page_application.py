"""Work-state Page queries and layout commands for the v2 application boundary.

WorkLifecycleRepository is the authority for opening/validating a Work.
Queries read a consistent SQLite snapshot. Commands delegate to the Page
repositories and never read or write legacy project/panel JSON files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from manga_autopilot.repositories.page_domain import (
    LayoutRepository,
    PageDomainNotFoundError,
)
from manga_autopilot.repositories.work_lifecycle import WorkLifecycleRepository
from manga_autopilot.storage.repository import repository_read

_JSON_FIELDS = frozenset({
    "parameters_json", "geometry_json", "constraints_json",
    "semantic_json", "safe_subject_region_json", "bubble_regions_json",
    "forbidden_regions_json", "action_json", "camera_json",
    "emotion_requirements_json", "environment_requirements_json",
    "continuity_requirements_json", "generation_spec_json",
})


def _decode(row: Any) -> dict[str, Any]:
    record = dict(row)
    for field in _JSON_FIELDS & record.keys():
        if record[field] is not None:
            record[field] = json.loads(record[field])
    return record


class PageApplicationService:
    """A Work-scoped query/command facade suitable for HTTP or a UI adapter."""

    def __init__(self, storage_root: str | Path) -> None:
        self.lifecycle = WorkLifecycleRepository(storage_root)

    def list_pages(self, work_id: str) -> list[dict[str, Any]]:
        handle = self.lifecycle.open_work(work_id)
        with repository_read(handle.database_path) as db:
            return [
                _decode(row) for row in db.execute(
                    "SELECT * FROM pages ORDER BY order_key, page_number, id"
                )
            ]

    def get_page(self, work_id: str, page_id: str) -> dict[str, Any]:
        handle = self.lifecycle.open_work(work_id)
        with repository_read(handle.database_path) as db:
            # Explicit BEGIN gives all four queries the same snapshot even
            # while an independent renderer or Editor writes to the Work.
            db.execute("BEGIN")
            try:
                page = db.execute(
                    "SELECT * FROM pages WHERE id = ?", (page_id,)
                ).fetchone()
                if page is None:
                    raise PageDomainNotFoundError(f"page not found: {page_id}")
                layout = db.execute(
                    "SELECT * FROM layout_instances WHERE page_id = ?", (page_id,)
                ).fetchone()
                slots = (
                    db.execute(
                        """
                        SELECT * FROM layout_slots WHERE layout_instance_id = ?
                        ORDER BY reading_order, id
                        """,
                        (layout["id"],),
                    ).fetchall()
                    if layout is not None
                    else []
                )
                panels = db.execute(
                    "SELECT * FROM panels WHERE page_id = ? ORDER BY order_index, id",
                    (page_id,),
                ).fetchall()
                result = {
                    "work_id": handle.work_id,
                    "page": _decode(page),
                    "layout": _decode(layout) if layout is not None else None,
                    "slots": [_decode(row) for row in slots],
                    "panels": [_decode(row) for row in panels],
                }
            finally:
                db.rollback()
        return result

    def update_layout(
        self,
        work_id: str,
        page_id: str,
        *,
        expected_revision: int,
        slot_updates: list[dict[str, Any]] | None = None,
        panel_bindings: list[dict[str, Any]] | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        handle = self.lifecycle.open_work(work_id)
        with repository_read(handle.database_path) as db:
            page = db.execute(
                "SELECT id FROM pages WHERE id = ?", (page_id,)
            ).fetchone()
            if page is None:
                raise PageDomainNotFoundError(f"page not found: {page_id}")
            row = db.execute(
                "SELECT id FROM layout_instances WHERE page_id = ?", (page_id,)
            ).fetchone()
            if row is None:
                raise PageDomainNotFoundError(
                    f"layout not found for page: {page_id}"
                )
            layout_id = str(row["id"])
        # Ownership is checked again under BEGIN IMMEDIATE by the repository.
        LayoutRepository(handle.database_path).update_layout(
            layout_id,
            expected_revision=expected_revision,
            slot_updates=slot_updates or (),
            panel_bindings=panel_bindings or (),
            **changes,
        )
        return self.get_page(work_id, page_id)


__all__ = ["PageApplicationService"]
