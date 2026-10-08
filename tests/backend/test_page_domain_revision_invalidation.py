"""W0004 Page-domain audit-trail and dependency-invalidation regression tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.repositories import (
    LayoutRepository,
    PageDomainAuditRepository,
    PageDomainOwnershipError,
    PageRepository,
    PanelRepository,
)
from manga_autopilot.storage import (
    WORK_MIGRATIONS,
    RevisionConflictError,
    bootstrap_work_database,
    migrate_work_database,
    read_work_identity,
    repository_read,
)


@pytest.fixture
def domain(tmp_path: Path):
    path = tmp_path / "work.sqlite3"
    bootstrap_work_database(path, work_id="work_v2")
    return (
        PageRepository(path),
        LayoutRepository(path),
        PanelRepository(path),
        PageDomainAuditRepository(path),
    )


def _seed(domain, suffix: str = "a", page_num: int = 1) -> None:
    pages, layouts, panels, _ = domain
    pages.create_page(
        page_id=f"page_{suffix}",
        page_number=page_num,
        order_key=f"{page_num:04d}",
        page_purpose="Intro",
    )
    layouts.create_layout(
        layout_id=f"layout_{suffix}",
        page_id=f"page_{suffix}",
        geometry={"width": 1000, "height": 1600},
    )
    layouts.create_slot(
        slot_id=f"slot_{suffix}",
        layout_id=f"layout_{suffix}",
        slot_key="top",
        reading_order=1,
        geometry={"x": 1, "y": 2, "width": 400, "height": 500},
    )
    panels.create_panel(
        panel_id=f"panel_{suffix}",
        page_id=f"page_{suffix}",
        order_index=1,
        panel_purpose="The reveal",
        layout_slot_id=f"slot_{suffix}",
        action={"character_id": "hero", "pose": "point"},
        generation_spec={"seed": 12},
    )


def _counts(pages: PageRepository) -> tuple[int, int, int]:
    with repository_read(pages.database_path) as db:
        return tuple(
            int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in ("commits", "entity_revisions", "invalidations")
        )


def _new_invalidations(audit: PageDomainAuditRepository, commit_seq: int):
    return [
        record for record in audit.list_invalidations()
        if record["created_commit_seq"] == commit_seq
    ]


def test_w0004_creates_canonical_invalidation_ledger_and_indexes(domain):
    pages, _, _, _ = domain
    with repository_read(pages.database_path) as connection:
        columns = {
            row["name"] for row in connection.execute(
                "PRAGMA table_info(invalidations)"
            )
        }
        indexes = {
            row["name"] for row in connection.execute(
                "PRAGMA index_list(invalidations)"
            )
        }
        targets = {
            row["table"] for row in connection.execute(
                "PRAGMA foreign_key_list(invalidations)"
            )
        }
    assert {
        "id", "source_entity_type", "source_entity_id", "source_revision",
        "target_type", "target_id", "invalidation_kind", "reason",
        "created_commit_seq", "resolved_commit_seq", "created_at", "resolved_at",
    } <= columns
    assert {"idx_invalidations_source", "idx_invalidations_target"} <= indexes
    assert targets == {"commits"}


def test_initial_entity_revisions_are_queryable_for_each_page_domain(domain):
    pages, layouts, panels, audit = domain
    _seed(domain)
    for kind, entity_id in (
        ("page", "page_a"),
        ("layout_instance", "layout_a"),
        ("layout_slot", "slot_a"),
        ("panel", "panel_a"),
    ):
        history = audit.list_entity_revisions(kind, entity_id)
        assert history[0]["entity_revision"] == 1
        assert history[0]["change_kind"] == "create"
        assert history[0]["before_json"] is None
        assert json.loads(history[0]["after_json"])["id"] == entity_id
        assert history[0]["commit_seq"] > 0
    page_history = audit.list_entity_revisions("page", "page_a")
    assert [row["entity_revision"] for row in page_history] == [1, 2]
    assert json.loads(page_history[1]["before_json"])["layout_instance_id"] is None
    assert json.loads(page_history[1]["after_json"])["layout_instance_id"] == "layout_a"
    assert page_history[1]["commit_seq"] == audit.list_entity_revisions(
        "layout_instance", "layout_a"
    )[0]["commit_seq"]
    assert pages.get_page("page_a")["revision"] == 2
    assert layouts.get_layout("layout_a")["revision"] == 1
    assert panels.get_panel("panel_a")["revision"] == 1


def test_geometry_batch_records_only_changed_layout_and_slot_and_marks_render_export(
    domain,
):
    pages, layouts, panels, audit = domain
    _seed(domain)
    original_panel = panels.get_panel("panel_a")
    before = _counts(pages)
    record = layouts.update_layout(
        "layout_a",
        expected_revision=1,
        geometry_json={"width": 1000, "height": 1900},
        slot_updates=[{
            "id": "slot_a",
            "expected_revision": 1,
            "geometry_json": {"x": 9, "y": 2, "width": 400, "height": 500},
        }],
    )
    assert record["revision"] == 2
    assert _counts(pages) == (before[0] + 1, before[1] + 2, before[2] + 4)
    assert panels.get_panel("panel_a") == original_panel
    assert pages.get_page("page_a")["revision"] == 2
    assert [row["entity_revision"] for row in audit.list_entity_revisions(
        "layout_instance", "layout_a"
    )] == [1, 2]
    assert [row["entity_revision"] for row in audit.list_entity_revisions(
        "layout_slot", "slot_a"
    )] == [1, 2]
    assert len(audit.list_entity_revisions("panel", "panel_a")) == 1
    fresh = _new_invalidations(audit, record["updated_commit_seq"])
    assert {row["target_type"] for row in fresh} == {
        "page_render", "page_export",
    }
    assert {row["source_entity_type"] for row in fresh} == {
        "layout_instance", "layout_slot",
    }
    assert not any("generation" in row["target_type"] for row in fresh)
    with repository_read(pages.database_path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_slot_only_change_keeps_layout_and_panel_history_unchanged(domain):
    pages, layouts, panels, audit = domain
    _seed(domain)
    before = _counts(pages)
    layout = layouts.update_layout(
        "layout_a",
        expected_revision=1,
        slot_updates=[{
            "id": "slot_a",
            "expected_revision": 1,
            "geometry_json": {"x": 14},
        }],
    )
    assert layout["revision"] == 1
    assert layouts.get_slot("slot_a")["revision"] == 2
    assert panels.get_panel("panel_a")["revision"] == 1
    assert _counts(pages) == (before[0] + 1, before[1] + 1, before[2] + 2)
    assert len(audit.list_entity_revisions("layout_instance", "layout_a")) == 1
    assert len(audit.list_entity_revisions("panel", "panel_a")) == 1


def test_panel_narrative_change_invalidates_only_relevant_downstream_work(domain):
    pages, layouts, panels, audit = domain
    _seed(domain)
    before = _counts(pages)
    old = panels.get_panel("panel_a")
    new = panels.update_panel(
        "panel_a",
        expected_revision=1,
        panel_purpose="A different revelation",
        action_json={"character_id": "hero", "pose": "run"},
    )
    assert _counts(pages) == (before[0] + 1, before[1] + 1, before[2] + 7)
    assert new["revision"] == 2
    assert new["id"] == old["id"]
    assert layouts.get_slot("slot_a")["revision"] == 1
    revisions = audit.list_entity_revisions("panel", "panel_a")
    assert [r["entity_revision"] for r in revisions] == [1, 2]
    assert json.loads(revisions[1]["before_json"])["action_json"] == old["action_json"]
    assert json.loads(revisions[1]["after_json"])["action_json"] == new["action_json"]
    assert revisions[1]["commit_seq"] == new["updated_commit_seq"]
    changed = _new_invalidations(audit, new["updated_commit_seq"])
    assert {r["target_type"] for r in changed} == {
        "panel_generation_spec",
        "panel_generation_candidates",
        "panel_selected_candidate_validity",
        "panel_qa",
        "page_render",
        "page_visual_qa",
        "page_export",
    }
    assert {r["source_entity_id"] for r in changed} == {"panel_a"}
    assert {r["source_revision"] for r in changed} == {2}
    assert {r["created_commit_seq"] for r in changed} == {
        new["updated_commit_seq"],
    }
    assert {r["resolved_commit_seq"] for r in changed} == {None}


def test_selected_candidate_change_does_not_invalidate_panel_generation(domain):
    pages, _, panels, audit = domain
    _seed(domain)
    before = _counts(pages)
    changed = panels.update_panel(
        "panel_a",
        expected_revision=1,
        selected_candidate_id="candidate_001",
    )
    assert _counts(pages) == (before[0] + 1, before[1] + 1, before[2] + 3)
    targets = {
        r["target_type"] for r in _new_invalidations(
            audit, changed["updated_commit_seq"]
        )
    }
    assert targets == {"page_render", "page_visual_qa", "page_export"}


def test_panel_placement_and_page_metadata_only_stale_render_export(domain):
    pages, _, panels, audit = domain
    _seed(domain)
    changed_panel = panels.update_panel(
        "panel_a", expected_revision=1, order_index=2
    )
    changed_page = pages.update_page(
        "page_a", expected_revision=2, page_number=2
    )
    for source in (changed_panel, changed_page):
        changed = _new_invalidations(audit, source["updated_commit_seq"])
        assert {r["target_type"] for r in changed} == {
            "page_render", "page_export",
        }


def test_noop_revisions_are_not_recorded_and_legacy_commits_retained(domain):
    pages, layouts, panels, audit = domain
    _seed(domain)
    before = _counts(pages)
    pages.update_page("page_a", expected_revision=2, page_purpose="Intro")
    layouts.update_layout("layout_a", expected_revision=1)
    panels.update_panel("panel_a", expected_revision=1, panel_purpose="The reveal")
    assert _counts(pages) == before
    assert pages.get_page("page_a")["revision"] == 2
    assert len(audit.list_entity_revisions("page", "page_a")) == 2


def test_ownership_failure_rolls_back_mutations_history_and_invalidations(domain):
    pages, layouts, panels, audit = domain
    _seed(domain)
    _seed(domain, "b", 2)
    before = _counts(pages)
    original_layout = layouts.get_layout("layout_a")
    original_slot = layouts.get_slot("slot_a")
    original_panel = panels.get_panel("panel_a")
    with pytest.raises(PageDomainOwnershipError):
        layouts.update_layout(
            "layout_a",
            expected_revision=1,
            geometry_json={"width": 99},
            slot_updates=[
                {"id": "slot_a", "expected_revision": 1, "geometry_json": {"x": 22}},
                {"id": "slot_b", "expected_revision": 1, "geometry_json": {"x": 33}},
            ],
        )
    assert _counts(pages) == before
    assert layouts.get_layout("layout_a") == original_layout
    assert layouts.get_slot("slot_a") == original_slot
    assert panels.get_panel("panel_a") == original_panel
    assert len(audit.list_entity_revisions("layout_instance", "layout_a")) == 1


def test_stale_revision_does_not_write_history_or_invalidations(domain):
    pages, layouts, panels, audit = domain
    _seed(domain)
    before = _counts(pages)
    with pytest.raises(RevisionConflictError):
        panels.update_panel(
            "panel_a", expected_revision=9,
            panel_purpose="Should not be applied",
        )
    with pytest.raises(RevisionConflictError):
        layouts.update_layout(
            "layout_a", expected_revision=2,
            geometry_json={"height": 1},
        )
    assert _counts(pages) == before
    assert len(audit.list_entity_revisions("panel", "panel_a")) == 1


def test_audit_queries_select_only_requested_entity_and_unresolved_targets(domain):
    pages, _, panels, audit = domain
    _seed(domain)
    _seed(domain, "b", 2)
    assert all(
        r["source_entity_id"] == "panel_b" for r in audit.list_invalidations(
            target_type="panel_qa", target_id="panel_b"
        )
    )
    assert not audit.list_entity_revisions("panel", "unknown")
    with pytest.raises(ValueError, match="unsupported"):
        audit.list_entity_revisions("unknown", "panel_a")
    assert audit.list_invalidations(target_type="page_export", target_id="page_b")
    assert audit.list_invalidations(target_type="page_export", target_id="page_a")
    assert all(
        record["resolved_commit_seq"] is None
        for record in audit.list_invalidations(unresolved_only=True)
    )


def test_upgrade_existing_w0003_work_preserves_data_and_historical_revisions(
    tmp_path: Path,
):
    db_path = tmp_path / "work.sqlite3"
    initial = bootstrap_work_database(
        db_path,
        work_id="work_before_w4",
        database_id="workdb_before_w4",
        migrations=WORK_MIGRATIONS[:3],
    )
    assert initial.migration.current_version == 3
    with repository_read(db_path) as db:
        w3_checksum = db.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 3"
        ).fetchone()[0]

    upgraded = migrate_work_database(
        db_path,
        work_id="work_before_w4",
        migrations=WORK_MIGRATIONS,
    )
    assert upgraded.applied_versions == (4,)
    assert upgraded.backup_path is not None
    assert upgraded.backup_path.is_file()
    assert read_work_identity(db_path).database_id == "workdb_before_w4"
    with repository_read(db_path) as db:
        assert db.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 3"
        ).fetchone()[0] == w3_checksum
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'pages'"
        ).fetchone() is not None
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'invalidations'"
        ).fetchone() is not None
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []

    page = PageRepository(db_path).create_page(
        page_id="after_upgrade", page_number=1,
        order_key="0001", page_purpose="Post-upgrade",
    )
    history = PageDomainAuditRepository(db_path).list_entity_revisions(
        "page", page["id"]
    )
    assert len(history) == 1
    assert history[0]["entity_revision"] == 1
    again = migrate_work_database(db_path, work_id="work_before_w4")
    assert again.applied_versions == ()
    assert again.backup_path is None
