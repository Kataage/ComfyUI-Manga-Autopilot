"""Tests for the page editor service."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manga_autopilot.models.page import PagePlan
from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.services.page_editor import (
    LayoutUpdate,
    PageEditorService,
    PageLayoutError,
    PageNotFoundError,
    ProjectNotFoundError,
)


def _init_project(tmp_path: Path, project_id: str = "demo") -> Path:
    project = tmp_path / "projects" / project_id
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.json").write_text(
        json.dumps({"project_id": project_id, "title": "Demo"}),
        encoding="utf-8",
    )
    return project


def test_for_project_raises_for_missing(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        PageEditorService.for_project(tmp_path, "missing")


def test_upsert_and_list_pages(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = PageEditorService.for_project(tmp_path, "demo")
    page = PagePlan(page_number=1, summary="intro", panel_count=2)
    svc.upsert_page(page)
    pages = svc.list_pages()
    assert len(pages) == 1 and pages[0].summary == "intro"


def test_upsert_replaces_existing(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = PageEditorService.for_project(tmp_path, "demo")
    svc.upsert_page(PagePlan(page_number=1, panel_count=1))
    svc.upsert_page(PagePlan(page_number=1, summary="updated", panel_count=1))
    assert svc.list_pages()[0].summary == "updated"


def test_get_page_missing(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = PageEditorService.for_project(tmp_path, "demo")
    with pytest.raises(PageNotFoundError):
        svc.get_page(1)


def test_delete_page_removes_panels(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = PageEditorService.for_project(tmp_path, "demo")
    svc.upsert_page(PagePlan(page_number=1, panel_count=1))
    svc.apply_template(1, "page_2_horizontal")
    assert len(svc.get_layout(1)) == 2
    svc.delete_page(1)
    assert svc.list_pages() == []
    assert svc.get_layout(1) == []


def test_apply_template(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = PageEditorService.for_project(tmp_path, "demo")
    layouts = svc.apply_template(1, "page_4_grid")
    assert len(layouts) == 4
    for layout in layouts:
        assert layout.width > 0
    # Idempotent reapply replaces, not stacks.
    layouts2 = svc.apply_template(1, "page_4_grid")
    assert len(svc.get_layout(1)) == 4
    assert len(layouts2) == 4


def test_update_layout(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = PageEditorService.for_project(tmp_path, "demo")
    update = LayoutUpdate(
        page_width=1000,
        page_height=1400,
        panels=[PanelLayout(panel_id="p1", x=10, y=20, width=300, height=200)],
    )
    out = svc.update_layout(1, update)
    assert out[0].panel_id == "p1"
    # Re-applying different layout replaces prior records.
    update2 = LayoutUpdate(
        page_width=1000,
        page_height=1400,
        panels=[
            PanelLayout(panel_id="a", x=0, y=0, width=100, height=100),
            PanelLayout(panel_id="b", x=100, y=0, width=100, height=100),
        ],
    )
    svc.update_layout(1, update2)
    assert len(svc.get_layout(1)) == 2


def test_update_layout_rejects_duplicate_ids(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = PageEditorService.for_project(tmp_path, "demo")
    bad_payload = {
        "page_width": 1000,
        "page_height": 1400,
        "panels": [
            PanelLayout(panel_id="same", width=100, height=100).model_dump(),
            PanelLayout(panel_id="same", width=100, height=100).model_dump(),
        ],
    }
    with pytest.raises(PageLayoutError):
        svc.update_layout(1, bad_payload)


def test_panel_records_round_trip(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = PageEditorService.for_project(tmp_path, "demo")
    svc.apply_template(1, "page_3_t")
    recs = svc.panel_records()
    assert len(recs) == 3
    assert {r.page_number for r in recs} == {1}


def test_corrupt_pages_file_raises(tmp_path: Path) -> None:
    project = _init_project(tmp_path)
    (project / "pages.json").write_text("not json", encoding="utf-8")
    svc = PageEditorService.for_project(tmp_path, "demo")
    with pytest.raises(PageLayoutError):
        svc.list_pages()
