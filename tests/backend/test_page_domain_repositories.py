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
    PageDomainPanelArchivedError,
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


@pytest.mark.parametrize("same_slot", [True, False])
def test_archived_panel_binding_rejected_by_domain_repo_before_any_write(
    repositories, same_slot,
):
    pages, layouts, panels = repositories
    _setup(repositories)
    layouts.create_slot(
        slot_id="slot_002", layout_id="layout_001", slot_key="bottom",
        reading_order=2, geometry={"x": 5, "y": 600},
    )
    panels.create_panel(
        panel_id="panel_live", page_id="page_001", order_index=2,
        layout_slot_id="slot_002", panel_purpose="Visible",
    )
    archived = panels.update_panel(
        "panel_001", expected_revision=1, archived_at="2026-10-08T13:00:00Z",
    )
    live = panels.get_panel("panel_live")
    slot = layouts.get_slot("slot_001")
    original_layout = layouts.get_layout("layout_001")
    with repository_read(pages.database_path) as conn:
        history_before = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        )
    with pytest.raises(PageDomainPanelArchivedError, match="unarchive"):
        layouts.update_layout(
            "layout_001", expected_revision=original_layout["revision"],
            geometry_json={"width": 880},
            slot_updates=[{
                "id": "slot_001", "expected_revision": slot["revision"],
                "geometry_json": {"x": 70},
            }],
            panel_bindings=[
                {
                    "id": "panel_live", "expected_revision": live["revision"],
                    "layout_slot_id": "slot_001",
                },
                {
                    "id": "panel_001",
                    "expected_revision": archived["revision"],
                    "layout_slot_id": (
                        "slot_001" if same_slot else "slot_002"
                    ),
                },
            ],
        )
    assert layouts.get_layout("layout_001") == original_layout
    assert layouts.get_slot("slot_001") == slot
    assert panels.get_panel("panel_001") == archived
    assert panels.get_panel("panel_live") == live
    with repository_read(pages.database_path) as conn:
        assert tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        ) == history_before
    revived = panels.update_panel(
        "panel_001", expected_revision=archived["revision"], archived_at=None,
    )
    changed = layouts.update_layout(
        "layout_001", expected_revision=original_layout["revision"],
        panel_bindings=[{
            "id": "panel_001",
            "expected_revision": revived["revision"],
            "layout_slot_id": "slot_002",
        }],
    )
    assert changed["revision"] == original_layout["revision"]
    assert panels.get_panel("panel_001")["layout_slot_id"] == "slot_002"
    assert panels.get_panel("panel_001")["revision"] == revived["revision"] + 1



def test_layout_atomic_snapshot_callback_observes_batch_and_noop_without_commit(
    repositories,
):
    pages, layouts, panels = repositories
    _setup(repositories)
    seen = []

    def snapshot(db):
        assert db.in_transaction
        row = db.execute(
            "SELECT revision, geometry_json FROM layout_instances WHERE id = ?",
            ("layout_001",),
        ).fetchone()
        slot = db.execute(
            "SELECT revision, geometry_json FROM layout_slots WHERE id = ?",
            ("slot_001",),
        ).fetchone()
        seen.append((row["revision"], slot["revision"]))
        return {
            "layout": dict(row),
            "slot": dict(slot),
        }

    before = _commits(pages)
    result = layouts.update_layout(
        "layout_001", expected_revision=1,
        slot_updates=[{
            "id": "slot_001", "expected_revision": 1,
            "geometry_json": {"x": 50, "y": 20, "width": 450, "height": 400},
        }],
        _read_snapshot=snapshot,
    )
    assert result["layout"]["revision"] == 1
    assert result["slot"]["revision"] == 2
    assert json.loads(result["slot"]["geometry_json"])["x"] == 50
    assert seen == [(1, 2)]
    assert _commits(pages) == before + 1
    assert panels.get_panel("panel_001")["layout_slot_id"] == "slot_001"

    unchanged = layouts.update_layout(
        "layout_001", expected_revision=1,
        _read_snapshot=snapshot,
    )
    assert unchanged == result
    assert seen == [(1, 2), (1, 2)]
    assert _commits(pages) == before + 1


def test_layout_atomic_snapshot_failure_rolls_back_batch_and_commit(repositories):
    pages, layouts, panels = repositories
    _setup(repositories)
    before = _commits(pages)
    old_layout = layouts.get_layout("layout_001")
    old_slot = layouts.get_slot("slot_001")
    old_panel = panels.get_panel("panel_001")

    def broken_reader(db):
        assert db.in_transaction
        row = db.execute(
            "SELECT revision FROM layout_instances WHERE id = 'layout_001'",
        ).fetchone()
        assert row["revision"] == 2
        raise RuntimeError("failed to materialize command response")

    with pytest.raises(RuntimeError, match="materialize command response"):
        layouts.update_layout(
            "layout_001", expected_revision=1,
            geometry_json={"width": 1550, "height": 1750},
            slot_updates=[{
                "id": "slot_001", "expected_revision": 1,
                "geometry_json": {"x": 30, "y": 30, "width": 400, "height": 400},
            }],
            panel_bindings=[{
                "id": "panel_001", "expected_revision": 1,
                "layout_slot_id": None,
            }],
            _read_snapshot=broken_reader,
        )
    assert _commits(pages) == before
    assert layouts.get_layout("layout_001") == old_layout
    assert layouts.get_slot("slot_001") == old_slot
    assert panels.get_panel("panel_001") == old_panel
    with repository_read(pages.database_path) as db:
        assert not db.execute(
            "SELECT 1 FROM commits WHERE operation_type = 'update_layout'"
        ).fetchone()
        assert not db.execute(
            "SELECT 1 FROM entity_revisions WHERE entity_type = 'layout_instance'"
            " AND entity_revision = 2 AND entity_id = 'layout_001'"
        ).fetchone()


@pytest.mark.parametrize(
    "operation",
    [
        "create_panel",
        "update_panel",
        "create_slot",
        "update_slot",
        "create_layout",
    ],
)
def test_phase_c_independent_audit_archived_page_rejects_child_mutations(
    repositories, operation: str,
) -> None:
    """Archived Pages are read-only until explicitly unarchived.

    Exercise public Work repository entrypoints directly: an archived Page's
    child revisions must not advance and no new children or invalidation
    history may be committed via a path other than the guarded Layout API.
    """
    from manga_autopilot.repositories.page_domain import PageDomainArchivedError

    pages, layouts, panels = repositories
    _setup(repositories)
    pages.create_page(
        page_id="page_empty", page_number=2,
        order_key="0002", page_purpose="Awaiting layout",
    )
    archived_page = pages.get_page(
        "page_empty" if operation == "create_layout" else "page_001"
    )
    target_page = archived_page["id"]
    pages.update_page(
        target_page, expected_revision=archived_page["revision"],
        archived_at="2026-10-11T00:00:00Z",
    )
    before_commits = _commits(pages)
    with repository_read(pages.database_path) as connection:
        before_ledger = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        )
    original_page = pages.get_page(target_page)
    original_panel = panels.get_panel("panel_001")
    original_slot = layouts.get_slot("slot_001")

    def mutate_archived_child() -> None:
        if operation == "create_panel":
            panels.create_panel(
                panel_id="panel_archived", page_id=target_page,
                order_index=2, panel_purpose="must not write",
            )
        elif operation == "update_panel":
            panels.update_panel(
                "panel_001", expected_revision=original_panel["revision"],
                panel_purpose="mutated after archive",
            )
        elif operation == "create_slot":
            layouts.create_slot(
                slot_id="slot_archived", layout_id="layout_001",
                slot_key="forbidden", reading_order=2,
                geometry={"x": 1, "y": 1, "width": 10, "height": 10},
            )
        elif operation == "update_slot":
            layouts.update_slot(
                "slot_001", expected_revision=original_slot["revision"],
                semantic_json={"altered": True},
            )
        else:
            layouts.create_layout(
                layout_id="layout_archived", page_id=target_page,
                geometry={"width": 1000, "height": 1000},
            )

    with pytest.raises(PageDomainArchivedError, match="archived|unarchive"):
        mutate_archived_child()
    assert _commits(pages) == before_commits, (
        "archived child mutation must not append a Work commit"
    )
    with repository_read(pages.database_path) as connection:
        after_ledger = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        )
    assert after_ledger == before_ledger, (
        "failed archived-Page mutations cannot append revisions or invalidations"
    )
    assert pages.get_page(target_page) == original_page
    assert panels.get_panel("panel_001") == original_panel
    assert layouts.get_slot("slot_001") == original_slot

    # Recovery/unarchive is deliberate and must restore ordinary child writes.
    released = pages.update_page(
        target_page, expected_revision=original_page["revision"], archived_at=None,
    )
    assert released["archived_at"] is None
    mutate_archived_child()
    assert _commits(pages) == before_commits + 2, (
        "exactly one Work commit to unarchive and one for the successful child edit"
    )


@pytest.mark.parametrize("operation", ["update_panel", "update_slot"])
def test_issue411_archived_page_rejects_even_noop_child_updates(
    repositories, operation: str,
) -> None:
    """No-op write paths must still respect an archived parent Page."""
    from manga_autopilot.repositories.page_domain import PageDomainArchivedError

    pages, layouts, panels = repositories
    _setup(repositories)
    page = pages.get_page("page_001")
    pages.update_page(
        "page_001", expected_revision=page["revision"],
        archived_at="2026-10-11T00:00:00Z",
    )
    before = _commits(pages)
    if operation == "update_panel":
        record = panels.get_panel("panel_001")
        with pytest.raises(PageDomainArchivedError, match="archived|unarchive"):
            panels.update_panel(
                "panel_001", expected_revision=record["revision"],
                panel_purpose=record["panel_purpose"],
            )
        assert panels.get_panel("panel_001") == record
    else:
        record = layouts.get_slot("slot_001")
        with pytest.raises(PageDomainArchivedError, match="archived|unarchive"):
            layouts.update_slot(
                "slot_001", expected_revision=record["revision"],
                semantic_json=json.loads(record["semantic_json"]),
            )
        assert layouts.get_slot("slot_001") == record
    assert _commits(pages) == before

@pytest.mark.parametrize("scope", ["page", "panel"])
@pytest.mark.parametrize("noop", [False, True])
def test_phase_c_independent_audit_archived_entity_rejects_direct_nonrecovery_updates(
    repositories, scope: str, noop: bool,
) -> None:
    """Archived entities must be read-only until an explicit unarchive command.

    Unlike #411's archived *parent* child-write fence, this exercises the
    public direct Page/Panel repositories with a non-archival semantic edit or
    a no-op update against an entity whose own archived_at is already set.
    Expected RED on the frozen merged post-#411 develop; no production edits.
    """
    from manga_autopilot.repositories.page_domain import PageDomainArchivedError

    pages, _, panels = repositories
    _setup(repositories)
    if scope == "page":
        entity = pages.get_page("page_001")
        archived = pages.update_page(
            "page_001", expected_revision=entity["revision"],
            archived_at="2026-10-11T02:00:00Z",
        )
        error_type = PageDomainArchivedError
        payload = {"page_purpose": (
            archived["page_purpose"] if noop else "hidden page mutation"
        )}

        def attempt() -> dict:
            return pages.update_page(
                "page_001", expected_revision=archived["revision"], **payload,
            )

        def read() -> dict:
            return pages.get_page("page_001")

        def unarchive() -> dict:
            return pages.update_page(
                "page_001", expected_revision=archived["revision"],
                archived_at=None,
            )
    else:
        entity = panels.get_panel("panel_001")
        archived = panels.update_panel(
            "panel_001", expected_revision=entity["revision"],
            archived_at="2026-10-11T02:00:00Z",
        )
        error_type = PageDomainPanelArchivedError
        payload = {"panel_purpose": (
            archived["panel_purpose"] if noop else "hidden panel mutation"
        )}

        def attempt() -> dict:
            return panels.update_panel(
                "panel_001", expected_revision=archived["revision"], **payload,
            )

        def read() -> dict:
            return panels.get_panel("panel_001")

        def unarchive() -> dict:
            return panels.update_panel(
                "panel_001", expected_revision=archived["revision"],
                archived_at=None,
            )

    with repository_read(pages.database_path) as connection:
        before_ledger = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        )

    with pytest.raises(error_type, match="archived|unarchive"):
        attempt()

    assert read() == archived, "archived entity changed despite rejection"
    with repository_read(pages.database_path) as connection:
        after_ledger = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        )
    assert after_ledger == before_ledger, (
        "archived direct writes must not change commit/revision/invalidation history"
    )

    reopened = unarchive()
    assert reopened["archived_at"] is None
    accepted = (
        pages.update_page("page_001", expected_revision=reopened["revision"], **payload)
        if scope == "page" else
        panels.update_panel("panel_001", expected_revision=reopened["revision"], **payload)
    )
    assert accepted["archived_at"] is None
    assert accepted["revision"] == reopened["revision"] + (0 if noop else 1)

@pytest.mark.parametrize("scope", ["page", "panel"])
@pytest.mark.parametrize("case", ["mixed_recovery", "rearchive", "empty", "stale_revision"])
def test_issue413_archived_entity_recovery_is_exclusive_and_revision_guarded(
    repositories, scope: str, case: str,
) -> None:
    """Archive recovery is not an opportunity to smuggle in other edits."""
    from manga_autopilot.repositories.page_domain import PageDomainArchivedError

    pages, _, panels = repositories
    _setup(repositories)
    is_page = scope == "page"
    record = pages.get_page("page_001") if is_page else panels.get_panel("panel_001")
    update = (
        lambda expected_revision, **changes: pages.update_page(
            "page_001", expected_revision=expected_revision, **changes,
        )
        if is_page else panels.update_panel(
            "panel_001", expected_revision=expected_revision, **changes,
        )
    )
    archived = update(
        record["revision"], archived_at="2026-10-11T03:00:00Z",
    )
    payload = {
        "mixed_recovery": (
            {"archived_at": None, "page_purpose": "hidden change"}
            if is_page else
            {"archived_at": None, "panel_purpose": "hidden change"}
        ),
        "rearchive": {"archived_at": "2026-10-11T04:00:00Z"},
        "empty": {},
        "stale_revision": {
            "page_purpose" if is_page else "panel_purpose": "stale hidden change",
        },
    }[case]
    with repository_read(pages.database_path) as conn:
        ledger = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        )
    expected = (archived["revision"] - 1) if case == "stale_revision" else archived["revision"]
    error = (
        RevisionConflictError if case == "stale_revision"
        else PageDomainArchivedError if is_page
        else PageDomainPanelArchivedError
    )
    with pytest.raises(error):
        update(expected, **payload)
    latest = pages.get_page("page_001") if is_page else panels.get_panel("panel_001")
    assert latest == archived
    with repository_read(pages.database_path) as conn:
        assert tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        ) == ledger


@pytest.mark.parametrize("scope", ["page", "panel"])
def test_issue413_archive_race_fences_later_semantic_update(
    repositories, monkeypatch, scope: str,
) -> None:
    """An archive holding BEGIN IMMEDIATE must win over a later edit."""
    import threading

    import manga_autopilot.repositories.page_domain as domain

    pages, _, panels = repositories
    _setup(repositories)
    is_page = scope == "page"
    target_table = "pages" if is_page else "panels"
    target_id = "page_001" if is_page else "panel_001"
    repo = pages if is_page else panels
    record = repo.get_page(target_id) if is_page else repo.get_panel(target_id)
    update = (
        lambda expected_revision, **changes: pages.update_page(
            target_id, expected_revision=expected_revision, **changes,
        )
        if is_page else panels.update_panel(
            target_id, expected_revision=expected_revision, **changes,
        )
    )
    at_archive_lock = threading.Event()
    allow_archive = threading.Event()
    attempted_edit = threading.Event()
    outcomes: dict[str, object] = {}
    original_patch = domain._patch
    stamp = "2026-10-11T05:00:00Z"

    def pause_archival_patch(conn, table, entity_id, **kwargs):
        if (
            table == target_table and entity_id == target_id
            and kwargs.get("attrs", {}).get("archived_at") == stamp
        ):
            at_archive_lock.set()
            if not allow_archive.wait(timeout=10):
                raise RuntimeError("archive test lock was not released")
        return original_patch(conn, table, entity_id, **kwargs)

    monkeypatch.setattr(domain, "_patch", pause_archival_patch)

    def archive_worker():
        try:
            outcomes["archive"] = update(record["revision"], archived_at=stamp)
        except BaseException as exc:
            outcomes["archive_error"] = exc

    def edit_worker():
        attempted_edit.set()
        try:
            outcomes["edit"] = update(
                record["revision"] + 1,
                **{"page_purpose" if is_page else "panel_purpose": "racy hidden edit"},
            )
        except BaseException as exc:
            outcomes["edit_error"] = exc

    archival = threading.Thread(target=archive_worker, daemon=True)
    editor = threading.Thread(target=edit_worker, daemon=True)
    archival.start()
    try:
        assert at_archive_lock.wait(timeout=5), "archive never acquired writer lock"
        editor.start()
        assert attempted_edit.wait(timeout=5), "edit never started"
    finally:
        allow_archive.set()
        archival.join(timeout=10)
        if editor.ident is not None:
            editor.join(timeout=10)
    assert not archival.is_alive() and not editor.is_alive()
    assert "archive_error" not in outcomes, outcomes
    assert "edit" not in outcomes, outcomes
    error_type = PageDomainArchivedError if is_page else PageDomainPanelArchivedError
    assert isinstance(outcomes.get("edit_error"), error_type), outcomes
    after = repo.get_page(target_id) if is_page else repo.get_panel(target_id)
    assert after == outcomes["archive"]
