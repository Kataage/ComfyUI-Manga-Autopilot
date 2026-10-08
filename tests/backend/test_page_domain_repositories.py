"""Repository integration tests for the W0003 Page, Layout, Slot, and Panel domain."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.repositories.page_domain import (
    LayoutRepository,
    PageDomainNotFoundError,
    PageDomainOwnershipError,
    PageRepository,
    PanelRepository,
)
from manga_autopilot.storage import (
    RevisionConflictError,
    bootstrap_work_database,
    repository_read,
)


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[PageRepository, LayoutRepository, PanelRepository]:
    database = tmp_path / "work.sqlite3"
    bootstrap_work_database(database, work_id="work_repo_test")
    return PageRepository(database), LayoutRepository(database), PanelRepository(database)


def _commits(pages: PageRepository) -> int:
    with repository_read(pages.database_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM commits").fetchone()[0])


def _setup(
    repositories: tuple[PageRepository, LayoutRepository, PanelRepository],
    *,
    page_id: str = "page_001",
    page_number: int = 1,
    layout_id: str = "layout_001",
    slot_id: str = "slot_001",
    panel_id: str = "panel_001",
) -> None:
    pages, layouts, panels = repositories
    pages.create_page(
        page_id=page_id, page_number=page_number,
        order_key=f"{page_number:04d}", page_purpose="Opening",
    )
    layouts.create_layout(
        layout_id=layout_id, page_id=page_id, geometry={"width": 1200, "height": 1600}
    )
    layouts.create_slot(
        slot_id=slot_id, layout_id=layout_id, slot_key="top",
        reading_order=1, geometry={"x": 0, "y": 0, "width": 500, "height": 400},
    )
    panels.create_panel(
        panel_id=panel_id, page_id=page_id,
        order_index=1, layout_slot_id=slot_id, panel_purpose="Introduce protagonist",
        action={"character_id": "hero", "pose": "wave"},
        generation_spec={"seed": 42},
    )


def test_page_create_get_list_update_and_optimistic_conflict(repositories):
    pages, _, _ = repositories
    first = pages.create_page(
        page_id="page_b", page_number=2, order_key="02", page_purpose="Ending",
    )
    second = pages.create_page(
        page_id="page_a", page_number=1, order_key="01", page_purpose="Beginning",
    )
    assert first["revision"] == second["revision"] == 1
    assert [page["id"] for page in pages.list_pages()] == ["page_a", "page_b"]
    assert pages.get_page("page_a")["page_purpose"] == "Beginning"

    before = _commits(pages)
    unchanged = pages.update_page(
        "page_a", expected_revision=1, page_purpose="Beginning"
    )
    assert unchanged["revision"] == 1
    assert _commits(pages) == before

    changed = pages.update_page(
        "page_a", expected_revision=1, page_purpose="New meaning"
    )
    assert changed["revision"] == 2
    assert changed["page_purpose"] == "New meaning"
    assert changed["updated_commit_seq"] > changed["created_commit_seq"]
    assert _commits(pages) == before + 1

    with pytest.raises(RevisionConflictError) as error:
        pages.update_page("page_a", expected_revision=1, page_purpose="stale")
    assert error.value.actual_revision == 2
    assert pages.get_page("page_a")["page_purpose"] == "New meaning"
    assert _commits(pages) == before + 1


def test_pages_enforce_uniqueness_and_immutable_identity(repositories):
    pages, _, _ = repositories
    pages.create_page(
        page_id="p1", page_number=1, order_key="a", page_purpose="First",
    )
    before = _commits(pages)
    with pytest.raises(sqlite3.IntegrityError):
        pages.create_page(
            page_id="p2", page_number=1, order_key="b", page_purpose="Collision",
        )
    with pytest.raises(ValueError, match="immutable"):
        pages.update_page("p1", expected_revision=1, id="overwrite")
    with pytest.raises(ValueError, match="positive integer"):
        pages.update_page("p1", expected_revision=1, page_number=True)
    assert _commits(pages) == before
    assert [r["id"] for r in pages.list_pages()] == ["p1"]


def test_layout_creation_sets_page_association_and_rejects_duplicate(repositories):
    pages, layouts, _ = repositories
    pages.create_page(page_id="p1", page_number=1, order_key="a", page_purpose="x")
    before = _commits(pages)
    layout = layouts.create_layout(layout_id="l1", page_id="p1")
    assert layout["revision"] == 1
    assert layouts.get_page_layout("p1")["id"] == "l1"
    page = pages.get_page("p1")
    assert page["layout_instance_id"] == "l1"
    assert page["revision"] == 2
    assert _commits(pages) == before + 1

    with pytest.raises(PageDomainOwnershipError):
        layouts.create_layout(layout_id="l2", page_id="p1")
    assert layouts.get_layout("l1")["id"] == "l1"
    assert _commits(pages) == before + 1


def test_layout_changes_preserve_panel_identity_semantics_and_unmodified_revisions(
    repositories,
):
    pages, layouts, panels = repositories
    _setup(repositories)
    page = pages.get_page("page_001")
    panel = panels.get_panel("panel_001")
    slot = layouts.get_slot("slot_001")
    original_panel = dict(panel)
    before = _commits(pages)
    result = layouts.update_layout(
        "layout_001", expected_revision=1,
        geometry_json={"height": 1800, "width": 1200},
        slot_updates=[{
            "id": "slot_001",
            "expected_revision": 1,
            "geometry_json": {"x": 9, "y": 2, "width": 550, "height": 400},
        }],
    )
    assert result["revision"] == 2
    assert json.loads(result["geometry_json"])["height"] == 1800
    assert _commits(pages) == before + 1
    assert layouts.get_slot("slot_001")["revision"] == slot["revision"] + 1
    assert layouts.get_slot("slot_001")["id"] == slot["id"]
    assert pages.get_page("page_001")["revision"] == page["revision"]
    assert panels.get_panel("panel_001") == original_panel
    assert json.loads(original_panel["generation_spec_json"]) == {"seed": 42}
    assert json.loads(original_panel["action_json"]) == {
        "character_id": "hero", "pose": "wave",
    }


def test_slot_only_mutation_does_not_advance_layout_or_panel_revision(repositories):
    pages, layouts, panels = repositories
    _setup(repositories)
    before = _commits(pages)
    layout = layouts.update_layout(
        "layout_001", expected_revision=1,
        slot_updates=[{
            "id": "slot_001", "expected_revision": 1,
            "geometry_json": {"x": 20, "width": 400},
        }],
    )
    assert layout["revision"] == 1
    assert layouts.get_slot("slot_001")["revision"] == 2
    assert panels.get_panel("panel_001")["revision"] == 1
    assert _commits(pages) == before + 1
    slot = layouts.update_slot(
        "slot_001", expected_revision=2,
        semantic_json={"visual_weight": "HIGH"},
    )
    assert slot["revision"] == 3
    assert layouts.get_layout("layout_001")["revision"] == 1
    assert panels.get_panel("panel_001")["revision"] == 1


def test_layout_batch_rollback_if_later_slot_belongs_to_another_page(repositories):
    pages, layouts, panels = repositories
    _setup(repositories)
    _setup(
        repositories, page_id="page_002", page_number=2, layout_id="layout_002",
        slot_id="slot_002", panel_id="panel_002",
    )
    before = _commits(pages)
    old_layout = layouts.get_layout("layout_001")
    old_slot = layouts.get_slot("slot_001")
    old_panel = panels.get_panel("panel_001")
    with pytest.raises(PageDomainOwnershipError):
        layouts.update_layout(
            "layout_001", expected_revision=1,
            geometry_json={"width": 999},
            slot_updates=[
                {
                    "id": "slot_001", "expected_revision": 1,
                    "geometry_json": {"x": 30},
                },
                {
                    "id": "slot_002", "expected_revision": 1,
                    "geometry_json": {"x": 40},
                },
            ],
        )
    assert _commits(pages) == before
    assert layouts.get_layout("layout_001") == old_layout
    assert layouts.get_slot("slot_001") == old_slot
    assert panels.get_panel("panel_001") == old_panel


def test_layout_batch_rolls_back_on_stale_slot_revision(repositories):
    pages, layouts, _ = repositories
    _setup(repositories)
    layouts.update_slot("slot_001", expected_revision=1, reading_order=2)
    before = _commits(pages)
    with pytest.raises(RevisionConflictError):
        layouts.update_layout(
            "layout_001", expected_revision=1,
            geometry_json={"height": 3000},
            slot_updates=[
                {"id": "slot_001", "expected_revision": 1, "geometry_json": {"x": 4}},
            ],
        )
    assert _commits(pages) == before
    assert layouts.get_layout("layout_001")["revision"] == 1
    assert json.loads(layouts.get_layout("layout_001")["geometry_json"]) == {
        "height": 1600, "width": 1200,
    }


def test_panel_create_and_update_reject_cross_page_slots(repositories):
    pages, layouts, panels = repositories
    _setup(repositories)
    _setup(
        repositories, page_id="page_002", page_number=2, layout_id="layout_002",
        slot_id="slot_002", panel_id="panel_002",
    )
    before = _commits(pages)
    panel_before = panels.get_panel("panel_001")
    with pytest.raises(PageDomainOwnershipError):
        panels.create_panel(
            panel_id="wrong", page_id="page_001", order_index=2,
            layout_slot_id="slot_002", panel_purpose="bad",
        )
    with pytest.raises(PageDomainOwnershipError):
        panels.update_panel(
            "panel_001", expected_revision=1, layout_slot_id="slot_002",
        )
    assert panels.get_panel("panel_001") == panel_before
    assert [p["id"] for p in panels.list_panels("page_001")] == ["panel_001"]
    assert _commits(pages) == before


def test_panel_semantic_update_keeps_identity_and_guards_revision(repositories):
    pages, layouts, panels = repositories
    _setup(repositories)
    before = _commits(pages)
    changed = panels.update_panel(
        "panel_001", expected_revision=1, panel_purpose="Twist",
        action_json={"pose": "turn"},
    )
    assert changed["revision"] == 2
    assert changed["id"] == "panel_001"
    assert changed["page_id"] == "page_001"
    assert changed["layout_slot_id"] == "slot_001"
    assert json.loads(changed["action_json"]) == {"pose": "turn"}
    assert _commits(pages) == before + 1
    with pytest.raises(RevisionConflictError):
        panels.update_panel(
            "panel_001", expected_revision=1, panel_purpose="Stale",
        )
    with pytest.raises(ValueError, match="immutable"):
        panels.update_panel(
            "panel_001", expected_revision=2, page_id="page_002",
        )
    assert panels.get_panel("panel_001")["panel_purpose"] == "Twist"
    assert layouts.get_slot("slot_001")["revision"] == 1


def test_invalid_json_payload_rolls_back_without_commit(repositories):
    pages, layouts, panels = repositories
    _setup(repositories)
    before = _commits(pages)
    with pytest.raises(ValueError, match="JSON object"):
        layouts.update_slot("slot_001", expected_revision=1, geometry_json="{}")
    with pytest.raises(ValueError, match="NaN"):
        panels.update_panel(
            "panel_001", expected_revision=1, action_json={"strength": float("nan")}
        )
    assert _commits(pages) == before
    assert layouts.get_slot("slot_001")["revision"] == 1
    assert panels.get_panel("panel_001")["revision"] == 1


def test_nonexistent_owner_and_record_are_rejected(repositories):
    pages, layouts, panels = repositories
    before = _commits(pages)
    with pytest.raises(PageDomainNotFoundError):
        layouts.create_layout(layout_id="missing_layout", page_id="nonexistent")
    with pytest.raises(PageDomainNotFoundError):
        panels.create_panel(
            panel_id="missing_panel", page_id="nonexistent",
            order_index=1, panel_purpose="none",
        )
    with pytest.raises(PageDomainNotFoundError):
        pages.get_page("missing")
    assert _commits(pages) == before


def test_layout_noop_and_duplicate_batch_rejected_without_history(repositories):
    pages, layouts, _ = repositories
    _setup(repositories)
    before = _commits(pages)
    assert layouts.update_layout("layout_001", expected_revision=1)["revision"] == 1
    assert _commits(pages) == before
    with pytest.raises(ValueError, match="duplicate slot update"):
        layouts.update_layout(
            "layout_001", expected_revision=1,
            slot_updates=[
                {"id": "slot_001", "expected_revision": 1, "geometry_json": {}},
                {"id": "slot_001", "expected_revision": 1, "geometry_json": {}},
            ],
        )
    assert _commits(pages) == before


def test_layout_slot_and_panel_order_constraints_preserved(repositories):
    pages, layouts, panels = repositories
    _setup(repositories)
    layouts.create_slot(
        slot_id="slot_002", layout_id="layout_001", slot_key="bottom",
        reading_order=2, geometry={"y": 50},
    )
    before = _commits(pages)
    with pytest.raises(sqlite3.IntegrityError):
        layouts.create_slot(
            slot_id="slot_collision", layout_id="layout_001", slot_key="other",
            reading_order=2, geometry={},
        )
    with pytest.raises(sqlite3.IntegrityError):
        panels.create_panel(
            panel_id="panel_collision", page_id="page_001",
            order_index=1, panel_purpose="collision",
        )
    assert _commits(pages) == before
    assert [s["id"] for s in layouts.list_slots("layout_001")] == [
        "slot_001", "slot_002",
    ]
