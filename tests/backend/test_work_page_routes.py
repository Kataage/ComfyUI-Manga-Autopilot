"""API acceptance tests for state-backed v2 Work Page queries and commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from manga_autopilot.repositories import (
    LayoutRepository,
    PageRepository,
    PanelRepository,
    WorkLifecycleRepository,
)
from manga_autopilot.routes import register_all
from manga_autopilot.storage import repository_read


@pytest.fixture()
async def api(aiohttp_client, tmp_path: Path):
    lifecycle = WorkLifecycleRepository(tmp_path)
    handle = lifecycle.create_work(title="Persistent manga", work_id="work_api_001")
    pages = PageRepository(handle.database_path)
    layouts = LayoutRepository(handle.database_path)
    panels = PanelRepository(handle.database_path)
    pages.create_page(
        page_id="page_001", page_number=1, order_key="0001",
        page_purpose="A quiet opening",
    )
    pages.create_page(
        page_id="page_002", page_number=2, order_key="0002",
        page_purpose="A new scene",
    )
    layouts.create_layout(
        layout_id="layout_001", page_id="page_001",
        geometry={"width": 1200, "height": 1600},
    )
    layouts.create_layout(
        layout_id="layout_002", page_id="page_002",
        geometry={"width": 1200},
    )
    layouts.create_slot(
        slot_id="slot_001", layout_id="layout_001",
        slot_key="top", reading_order=1,
        geometry={"x": 0, "y": 0, "width": 600, "height": 500},
    )
    layouts.create_slot(
        slot_id="slot_002", layout_id="layout_001",
        slot_key="bottom", reading_order=2,
        geometry={"x": 0, "y": 500, "width": 600, "height": 500},
    )
    layouts.create_slot(
        slot_id="other_slot", layout_id="layout_002",
        slot_key="other", reading_order=1, geometry={"x": 0},
    )
    panels.create_panel(
        panel_id="panel_001", page_id="page_001",
        order_index=1, layout_slot_id="slot_001",
        panel_purpose="The hero enters",
        action={"character_id": "hero", "pose": "wave"},
        generation_spec={"seed": 12},
    )
    panels.create_panel(
        panel_id="panel_002", page_id="page_001",
        order_index=2, layout_slot_id="slot_002",
        panel_purpose="A clue",
        action={"character_id": "hero", "pose": "look"},
    )
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    client = await aiohttp_client(app)
    prefix = "/manga_autopilot/api/v2/works/work_api_001/pages"
    return client, prefix, tmp_path, handle


async def _state(client, prefix: str):
    response = await client.get(prefix + "/page_001")
    return response, await response.json()


async def test_page_snapshot_contains_only_persisted_entities(api):
    client, prefix, _, _ = api
    response, body = await _state(client, prefix)
    assert response.status == 200
    assert body["work_id"] == "work_api_001"
    assert body["page"]["id"] == "page_001"
    assert body["page"]["layout_instance_id"] == "layout_001"
    assert body["layout"]["revision"] == 1
    assert body["layout"]["geometry_json"] == {"width": 1200, "height": 1600}
    assert [slot["id"] for slot in body["slots"]] == ["slot_001", "slot_002"]
    assert [p["id"] for p in body["panels"]] == ["panel_001", "panel_002"]
    assert body["panels"][0]["action_json"] == {
        "character_id": "hero", "pose": "wave",
    }
    assert body["panels"][0]["generation_spec_json"] == {"seed": 12}
    assert len({p["id"] for p in body["panels"]}) == 2
    response = await client.get(prefix)
    assert response.status == 200
    assert [p["id"] for p in (await response.json())["pages"]] == [
        "page_001", "page_002",
    ]


async def test_layout_and_binding_update_fetch_reload_preserves_panel_semantics(api):
    client, prefix, _, _ = api
    _, before = await _state(client, prefix)
    result = await client.patch(
        prefix + "/page_001/layout",
        json={
            "expected_revision": 1,
            "geometry_json": {"width": 1800, "height": 2200},
            "slot_updates": [{
                "id": "slot_001", "expected_revision": 1,
                "geometry_json": {"x": 10, "y": 20, "width": 900},
            }],
            "panel_bindings": [{
                "id": "panel_001", "expected_revision": 1,
                "layout_slot_id": "slot_002",
            }],
        },
    )
    assert result.status == 200
    changed = await result.json()
    assert changed["layout"]["revision"] == 2
    assert changed["layout"]["geometry_json"]["width"] == 1800
    assert changed["slots"][0]["revision"] == 2
    assert changed["panels"][0]["layout_slot_id"] == "slot_002"
    assert changed["panels"][0]["revision"] == 2
    assert changed["panels"][0]["action_json"] == before["panels"][0]["action_json"]
    assert changed["panels"][0]["generation_spec_json"] == (
        before["panels"][0]["generation_spec_json"]
    )
    assert changed["panels"][1] == before["panels"][1]
    assert changed["page"] == before["page"]
    reloaded_resp, reloaded = await _state(client, prefix)
    assert reloaded_resp.status == 200
    assert reloaded == changed


async def test_rebooted_application_reopens_same_work_state(api, aiohttp_client):
    client, prefix, tmp_path, _ = api
    update = await client.patch(
        prefix + "/page_001/layout",
        json={
            "expected_revision": 1,
            "panel_bindings": [{
                "id": "panel_002", "expected_revision": 1,
                "layout_slot_id": "slot_001",
            }],
        },
    )
    assert update.status == 200
    state = await update.json()
    restarted_app = web.Application()
    register_all(restarted_app, storage_root=str(tmp_path))
    restarted = await aiohttp_client(restarted_app)
    response = await restarted.get(prefix + "/page_001")
    assert response.status == 200
    assert await response.json() == state


async def test_stale_layout_revision_returns_machine_readable_409(api):
    client, prefix, _, _ = api
    first = await client.patch(
        prefix + "/page_001/layout",
        json={"expected_revision": 1, "geometry_json": {"height": 1800}},
    )
    assert first.status == 200
    _, saved = await _state(client, prefix)
    stale = await client.patch(
        prefix + "/page_001/layout",
        json={"expected_revision": 1, "geometry_json": {"height": 20}},
    )
    assert stale.status == 409
    problem = await stale.json()
    assert problem["error"] == "revision_conflict"
    assert problem["entity_type"] == "layout_instances"
    assert problem["entity_id"] == "layout_001"
    assert problem["expected_revision"] == 1
    assert problem["actual_revision"] == 2
    _, after = await _state(client, prefix)
    assert saved == after


async def test_stale_panel_binding_revision_rolls_back_layout_update(api):
    client, prefix, _, _ = api
    _, before = await _state(client, prefix)
    response = await client.patch(
        prefix + "/page_001/layout",
        json={
            "expected_revision": 1,
            "geometry_json": {"width": 10},
            "panel_bindings": [{
                "id": "panel_001", "expected_revision": 99,
                "layout_slot_id": "slot_002",
            }],
        },
    )
    assert response.status == 409
    assert (await response.json())["entity_type"] == "panels"
    _, after = await _state(client, prefix)
    assert after == before


async def test_wrong_page_slot_assignment_is_rejected_atomically(api):
    client, prefix, _, handle = api
    _, before = await _state(client, prefix)
    with repository_read(handle.database_path) as db:
        revisions_before = db.execute(
            "SELECT COUNT(*) FROM entity_revisions"
        ).fetchone()[0]
        invalidations_before = db.execute(
            "SELECT COUNT(*) FROM invalidations"
        ).fetchone()[0]
    response = await client.patch(
        prefix + "/page_001/layout",
        json={
            "expected_revision": 1,
            "geometry_json": {"width": 999},
            "panel_bindings": [{
                "id": "panel_001", "expected_revision": 1,
                "layout_slot_id": "other_slot",
            }],
        },
    )
    assert response.status == 409
    assert (await response.json())["error"] == "ownership_conflict"
    _, after = await _state(client, prefix)
    assert after == before
    with repository_read(handle.database_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM entity_revisions"
        ).fetchone()[0] == revisions_before
        assert db.execute(
            "SELECT COUNT(*) FROM invalidations"
        ).fetchone()[0] == invalidations_before


async def test_no_layout_fabrication_and_missing_entities_return_404(api):
    client, prefix, _, handle = api
    pages = PageRepository(handle.database_path)
    pages.create_page(
        page_id="empty_page", page_number=3,
        order_key="0003", page_purpose="No layout",
    )
    response = await client.get(prefix + "/empty_page")
    assert response.status == 200
    body = await response.json()
    assert body["layout"] is None
    assert body["slots"] == []
    assert body["panels"] == []
    missing = await client.get(prefix + "/missing_page")
    assert missing.status == 404
    missing_work = await client.get(
        "/manga_autopilot/api/v2/works/work_missing/pages/page_001"
    )
    assert missing_work.status == 404
    patch = await client.patch(
        prefix + "/empty_page/layout",
        json={"expected_revision": 1},
    )
    assert patch.status == 404


@pytest.mark.parametrize("body", [
    {"geometry_json": {"width": 10}},
    {"expected_revision": True},
    {"expected_revision": 0},
    {"expected_revision": 1, "slot_updates": "incorrect"},
    {"expected_revision": 1, "panel_bindings": [0]},
    {"expected_revision": 1, "page_id": "page_002"},
    {"expected_revision": 1, "geometry_json": "{ }"},
])
async def test_invalid_command_rejected_without_state_changes(api, body):
    client, prefix, _, _ = api
    _, before = await _state(client, prefix)
    response = await client.patch(prefix + "/page_001/layout", json=body)
    assert response.status == 400
    _, after = await _state(client, prefix)
    assert before == after


async def test_archived_page_hidden_by_default_but_available_for_explicit_recovery(
    api, aiohttp_client,
):
    client, prefix, tmp_path, handle = api
    pages = PageRepository(handle.database_path)
    panels = PanelRepository(handle.database_path)
    page = pages.get_page("page_001")
    pages.update_page(
        "page_001", expected_revision=page["revision"],
        archived_at="2026-10-08T10:00:00Z",
    )
    active = await client.get(prefix)
    assert active.status == 200
    assert [p["id"] for p in (await active.json())["pages"]] == ["page_002"]
    archived_list = await client.get(prefix + "?include_archived=1")
    assert archived_list.status == 200
    assert [p["id"] for p in (await archived_list.json())["pages"]] == [
        "page_001", "page_002",
    ]
    blocked = await client.get(prefix + "/page_001")
    assert blocked.status == 409
    assert (await blocked.json())["error"] == "page_archived"
    recovery = await client.get(prefix + "/page_001?include_archived=1")
    assert recovery.status == 200
    original = await recovery.json()
    assert original["page"]["archived_at"] is not None
    assert len(original["panels"]) == 2

    # Archived Page commands must fail at the domain write lock, even with
    # correct Layout revisions (not merely fail in the UI preflight).
    before = pages.get_page("page_001")
    with repository_read(handle.database_path) as db:
        old_commits = db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    patch = await client.patch(
        prefix + "/page_001/layout",
        json={"expected_revision": 1, "geometry_json": {"width": 800}},
    )
    assert patch.status == 409
    assert (await patch.json())["error"] == "page_archived"
    assert pages.get_page("page_001") == before
    with repository_read(handle.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == old_commits

    # Archive/unarchive keeps the same IDs, Panel rows and persisted geometry.
    pages.update_page(
        "page_001", expected_revision=before["revision"], archived_at=None,
    )
    response, active_page = await _state(client, prefix)
    assert response.status == 200
    assert active_page["page"]["id"] == "page_001"
    assert len(active_page["panels"]) == 2
    assert panels.get_panel("panel_001")["page_id"] == "page_001"
    restarted = web.Application()
    register_all(restarted, storage_root=str(tmp_path))
    reopened = await aiohttp_client(restarted)
    assert (await (await reopened.get(prefix)).json())["pages"][0]["id"] == "page_001"


async def test_archived_panel_excluded_from_default_editor_projection(api):
    client, prefix, _, handle = api
    panels = PanelRepository(handle.database_path)
    original = panels.get_panel("panel_001")
    panels.update_panel(
        "panel_001", expected_revision=original["revision"],
        archived_at="2026-10-08T10:01:00Z",
    )
    response, body = await _state(client, prefix)
    assert response.status == 200
    assert [p["id"] for p in body["panels"]] == ["panel_002"]
    recovery = await client.get(prefix + "/page_001?include_archived=1")
    assert recovery.status == 200
    assert [p["id"] for p in (await recovery.json())["panels"]] == [
        "panel_001", "panel_002",
    ]
    after = panels.get_panel("panel_001")
    panels.update_panel(
        "panel_001", expected_revision=after["revision"], archived_at=None,
    )
    assert [p["id"] for p in (await _state(client, prefix))[1]["panels"]] == [
        "panel_001", "panel_002",
    ]


@pytest.mark.parametrize("query", ["?include_archived=true", "?include_archived=yes"])
async def test_invalid_archive_query_is_rejected(api, query):
    client, prefix, _, _ = api
    list_response = await client.get(prefix + query)
    assert list_response.status == 400
    detail_response = await client.get(prefix + "/page_001" + query)
    assert detail_response.status == 400


async def test_archived_panel_binding_is_rejected_atomically_and_unarchive_recovers(
    api, aiohttp_client,
):
    client, prefix, root, handle = api
    panels = PanelRepository(handle.database_path)
    layouts = LayoutRepository(handle.database_path)
    existing = panels.get_panel("panel_001")
    archived = panels.update_panel(
        "panel_001", expected_revision=existing["revision"],
        archived_at="2026-10-08T13:00:00Z",
    )

    active_response, active = await _state(client, prefix)
    assert active_response.status == 200
    assert [p["id"] for p in active["panels"]] == ["panel_002"]
    recovery = await client.get(prefix + "/page_001?include_archived=1")
    assert recovery.status == 200
    full = await recovery.json()
    assert [p["id"] for p in full["panels"]] == ["panel_001", "panel_002"]
    assert full["panels"][0]["archived_at"] is not None
    assert full["panels"][0]["layout_slot_id"] == "slot_001"

    def history_counts():
        with repository_read(handle.database_path) as db:
            return tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("commits", "entity_revisions", "invalidations")
            )

    before_counts = history_counts()
    before_layout = layouts.get_layout("layout_001")
    before_slot = layouts.get_slot("slot_001")
    before_live = panels.get_panel("panel_002")

    # Even a no-op command against a hidden Panel is not an active edit.
    noop = await client.patch(prefix + "/page_001/layout", json={
        "expected_revision": before_layout["revision"],
        "panel_bindings": [{
            "id": "panel_001", "expected_revision": archived["revision"],
            "layout_slot_id": archived["layout_slot_id"],
        }],
    })
    assert noop.status == 409
    assert (await noop.json())["error"] == "panel_archived"
    assert history_counts() == before_counts

    # Stage another valid Panel binding, a Slot update, and Layout geometry
    # before the archived participant: the whole batch must roll back.
    mixed = await client.patch(prefix + "/page_001/layout", json={
        "expected_revision": before_layout["revision"],
        "geometry_json": {"width": 1600, "height": 2200},
        "slot_updates": [{
            "id": "slot_001",
            "expected_revision": before_slot["revision"],
            "geometry_json": {"x": 22, "y": 35, "width": 500, "height": 400},
        }],
        "panel_bindings": [
            {
                "id": "panel_002",
                "expected_revision": before_live["revision"],
                "layout_slot_id": "slot_001",
            },
            {
                "id": "panel_001",
                "expected_revision": archived["revision"],
                "layout_slot_id": "slot_002",
            },
        ],
    })
    assert mixed.status == 409
    assert (await mixed.json())["error"] == "panel_archived"
    assert history_counts() == before_counts
    assert layouts.get_layout("layout_001") == before_layout
    assert layouts.get_slot("slot_001") == before_slot
    assert panels.get_panel("panel_001") == archived
    assert panels.get_panel("panel_002") == before_live
    recovery_after = await client.get(prefix + "/page_001?include_archived=1")
    assert await recovery_after.json() == full

    # The archived state can be explicitly and revision-safely reversed.
    revived = panels.update_panel(
        "panel_001", expected_revision=archived["revision"],
        archived_at=None,
    )
    accepted = await client.patch(prefix + "/page_001/layout", json={
        "expected_revision": before_layout["revision"],
        "geometry_json": {"width": 1600, "height": 2200},
        "slot_updates": [{
            "id": "slot_001",
            "expected_revision": before_slot["revision"],
            "geometry_json": {"x": 22, "y": 35, "width": 500, "height": 400},
        }],
        "panel_bindings": [
            {
                "id": "panel_002",
                "expected_revision": before_live["revision"],
                "layout_slot_id": "slot_001",
            },
            {
                "id": "panel_001",
                "expected_revision": revived["revision"],
                "layout_slot_id": "slot_002",
            },
        ],
    })
    assert accepted.status == 200, await accepted.text()
    saved = await accepted.json()
    assert saved["layout"]["geometry_json"]["width"] == 1600
    assert saved["slots"][0]["geometry_json"]["x"] == 22
    assert {p["id"]: p["layout_slot_id"] for p in saved["panels"]} == {
        "panel_001": "slot_002", "panel_002": "slot_001",
    }
    assert saved["panels"][0]["action_json"] == full["panels"][0]["action_json"]
    assert saved["panels"][0]["generation_spec_json"] == (
        full["panels"][0]["generation_spec_json"]
    )

    reopened_app = web.Application()
    register_all(reopened_app, storage_root=str(root))
    restarted = await aiohttp_client(reopened_app)
    reopened = await restarted.get(prefix + "/page_001")
    assert reopened.status == 200
    assert await reopened.json() == saved



async def test_layout_patch_ack_is_pre_archive_snapshot_even_if_page_archived_after_commit(
    api, monkeypatch,
):
    client, prefix, _, handle = api
    pages = PageRepository(handle.database_path)
    layout_repo = LayoutRepository(handle.database_path)
    _, original_state = await _state(client, prefix)
    original_revision = original_state["layout"]["revision"]
    original_page = original_state["page"]
    original_update = LayoutRepository.update_layout
    archive_events = []

    def commit_then_archive(self, layout_id, **kwargs):
        # Inject a *real* later independent transaction after the first
        # repository write commits but before the HTTP route constructs its
        # response. The former service post-commit get_page() returns HTTP 409.
        command_result = original_update(self, layout_id, **kwargs)
        current_page = pages.get_page("page_001")
        archived = pages.update_page(
            "page_001", expected_revision=current_page["revision"],
            archived_at="2026-10-08T14:00:00Z",
        )
        archive_events.append(archived)
        return command_result

    monkeypatch.setattr(LayoutRepository, "update_layout", commit_then_archive)
    with repository_read(handle.database_path) as db:
        commits_before = db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    response = await client.patch(
        prefix + "/page_001/layout",
        json={
            "expected_revision": original_revision,
            "geometry_json": {"width": 1750, "height": 2100},
        },
    )
    assert response.status == 200, await response.text()
    accepted = await response.json()
    assert len(archive_events) == 1
    assert accepted["page"] == original_page
    assert accepted["page"]["archived_at"] is None
    assert accepted["layout"]["revision"] == original_revision + 1
    assert accepted["layout"]["geometry_json"] == {
        "width": 1750, "height": 2100,
    }
    assert accepted["work_id"] == "work_api_001"
    assert [panel["id"] for panel in accepted["panels"]] == [
        "panel_001", "panel_002",
    ]
    assert accepted["layout"]["updated_commit_seq"] < (
        archive_events[0]["updated_commit_seq"]
    )
    with repository_read(handle.database_path) as db:
        # Exactly one Layout commit, followed by one Page archive commit.
        assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == (
            commits_before + 2
        )
        revisions = [
            dict(r) for r in db.execute(
                """SELECT entity_type, entity_id, commit_seq
                   FROM entity_revisions
                   WHERE commit_seq > ? ORDER BY commit_seq""",
                (accepted["layout"]["created_commit_seq"],),
            )
        ]
    assert any(row["entity_type"] == "layout_instance" for row in revisions)
    assert any(row["entity_type"] == "page" for row in revisions)

    normal = await client.get(prefix + "/page_001")
    assert normal.status == 409
    assert (await normal.json())["error"] == "page_archived"
    archived_resp = await client.get(
        prefix + "/page_001?include_archived=1"
    )
    assert archived_resp.status == 200
    latest = await archived_resp.json()
    assert latest["layout"] == accepted["layout"]
    assert latest["page"]["archived_at"] is not None
    assert latest["page"]["updated_commit_seq"] > (
        accepted["layout"]["updated_commit_seq"]
    )
    assert layout_repo.get_layout("layout_001")["revision"] == original_revision + 1


async def test_layout_patch_ack_excludes_later_layout_slot_and_panel_commit(
    api, monkeypatch,
):
    client, prefix, _, handle = api
    _, before = await _state(client, prefix)
    original_update = LayoutRepository.update_layout
    updates = []

    def commit_then_edit_again(self, layout_id, **kwargs):
        first_snapshot = original_update(self, layout_id, **kwargs)
        # A second genuine Work write lands after the first commit. It may
        # update Layout, Slot and Panel in the same transaction, independently
        # of the first caller's already accepted edit.
        latest_layout = LayoutRepository(handle.database_path).get_layout(layout_id)
        latest_slot = LayoutRepository(handle.database_path).get_slot("slot_001")
        latest_panel = PanelRepository(handle.database_path).get_panel("panel_001")
        original_update(
            self, layout_id, expected_revision=latest_layout["revision"],
            geometry_json={"width": 1999, "height": 2000},
            slot_updates=[{
                "id": "slot_001",
                "expected_revision": latest_slot["revision"],
                "geometry_json": {"x": 90, "y": 90, "width": 200, "height": 200},
            }],
            panel_bindings=[{
                "id": "panel_001",
                "expected_revision": latest_panel["revision"],
                "layout_slot_id": "slot_001",
            }],
        )
        updates.append(True)
        return first_snapshot

    monkeypatch.setattr(LayoutRepository, "update_layout", commit_then_edit_again)
    with repository_read(handle.database_path) as db:
        before_commits = db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
    result = await client.patch(
        prefix + "/page_001/layout",
        json={
            "expected_revision": before["layout"]["revision"],
            "geometry_json": {"width": 1700, "height": 2100},
            "slot_updates": [{
                "id": "slot_001", "expected_revision": before["slots"][0]["revision"],
                "geometry_json": {"x": 70, "y": 45, "width": 300, "height": 300},
            }],
            "panel_bindings": [{
                "id": "panel_001",
                "expected_revision": before["panels"][0]["revision"],
                "layout_slot_id": "slot_002",
            }],
        },
    )
    assert result.status == 200, await result.text()
    accepted = await result.json()
    assert updates == [True]
    assert accepted["layout"]["geometry_json"]["width"] == 1700
    assert accepted["layout"]["revision"] == before["layout"]["revision"] + 1
    assert accepted["slots"][0]["geometry_json"]["x"] == 70
    assert accepted["slots"][0]["revision"] == before["slots"][0]["revision"] + 1
    assert accepted["panels"][0]["layout_slot_id"] == "slot_002"
    assert accepted["panels"][0]["revision"] == before["panels"][0]["revision"] + 1
    assert accepted["panels"][0]["action_json"] == before["panels"][0]["action_json"]
    assert accepted["page"] == before["page"]

    fresh_resp, latest = await _state(client, prefix)
    assert fresh_resp.status == 200
    assert latest["layout"]["geometry_json"]["width"] == 1999
    assert latest["slots"][0]["geometry_json"]["x"] == 90
    assert latest["panels"][0]["layout_slot_id"] == "slot_001"
    assert latest["layout"]["revision"] == accepted["layout"]["revision"] + 1
    assert latest["slots"][0]["revision"] == accepted["slots"][0]["revision"] + 1
    assert latest["panels"][0]["revision"] == accepted["panels"][0]["revision"] + 1
    assert latest["layout"]["updated_commit_seq"] > (
        accepted["layout"]["updated_commit_seq"]
    )
    with repository_read(handle.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == (
            before_commits + 2
        )
        affected = {
            row["target_type"] for row in db.execute(
                "SELECT target_type FROM invalidations WHERE created_commit_seq = ?",
                (accepted["layout"]["updated_commit_seq"],),
            )
        }
    assert {"page_render", "page_export"} <= affected


@pytest.mark.asyncio
async def test_page_http_mutation_conflicts_with_live_autopilot_work_lease(api):
    """Two independent clients of one Work cannot silently write concurrently."""
    client, prefix, root, handle = api
    import asyncio

    from manga_autopilot.repositories.durable_runs import DurableRunRepository
    from manga_autopilot.services.autopilot import OrchestratorHooks
    from manga_autopilot.services.durable_autopilot import DurableAutopilotOrchestrator

    repo = DurableRunRepository(handle.database_path)
    run_id = repo.create_run(
        run_kind="AUTOPILOT", scope_type="WORK", scope_id=handle.work_id,
        requested_by="lease-conflict-test", input_fingerprint="lease-test",
    )["id"]
    started = asyncio.Event()
    finish = asyncio.Event()

    async def long_generation(_):
        started.set()
        await finish.wait()
        return {"ok": True}

    execution = asyncio.create_task(DurableAutopilotOrchestrator(
        repository=repo, work_id=handle.work_id,
        hooks=OrchestratorHooks(validate_input=long_generation),
        allow_omitted_hooks=True, lease_ttl_seconds=6,
    ).execute(run_id, input_payload={}, step_inputs={},
              lease_owner="active_autopilot"))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        current_response, original = await _state(client, prefix)
        assert current_response.status == 200
        revision = original["layout"]["revision"]
        response = await client.patch(
            prefix + "/page_001/layout",
            json={"expected_revision": revision,
                  "geometry_json": {"width": 1301, "height": 1600}},
        )
        payload = await response.json()
        assert response.status == 409, payload
        assert payload["error"] == "work_mutation_busy"
        after_response, after = await _state(client, prefix)
        assert after_response.status == 200
        assert after["layout"]["revision"] == revision
        assert after["layout"]["geometry_json"] == original["layout"]["geometry_json"]
        assert repo.inspect_lease(handle.work_id)["lease_owner"] == "active_autopilot"
    finally:
        finish.set()
        await asyncio.wait_for(execution, timeout=20)
    assert repo.inspect_lease(handle.work_id) is None


@pytest.mark.asyncio
async def test_issue405_page_read_keeps_event_loop_heartbeat_responsive(
    api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated slow SQLite/Work open must not block unrelated lease ticks.

    A separate watcher thread waits until an actual Page read begins, schedules
    a harmless event-loop pulse and releases the read after observing whether
    that pulse ran *during* its I/O. The watcher always releases the read; this
    test cannot hang if the route incorrectly blocks the event loop.
    """
    import asyncio
    import threading

    from manga_autopilot.services.page_application import PageApplicationService

    client, prefix, _, _ = api
    loop = asyncio.get_running_loop()
    started = threading.Event()
    release = threading.Event()
    heartbeat_pulse = threading.Event()
    observed: dict[str, bool] = {}
    original_get = PageApplicationService.get_page

    def slow_page_read(self, *args, **kwargs):
        started.set()
        if not release.wait(timeout=15):
            raise TimeoutError("audit-controlled slow Page read was not released")
        return original_get(self, *args, **kwargs)

    def independent_lease_tick() -> None:
        if not started.wait(timeout=10):
            observed["pulse_during_io"] = False
            release.set()
            return
        try:
            loop.call_soon_threadsafe(heartbeat_pulse.set)
            observed["pulse_during_io"] = heartbeat_pulse.wait(timeout=3)
        finally:
            release.set()

    monkeypatch.setattr(PageApplicationService, "get_page", slow_page_read)
    watcher = threading.Thread(target=independent_lease_tick, daemon=True)
    watcher.start()
    try:
        response = await client.get(prefix + "/page_001")
        assert response.status == 200
        assert (await response.json())["page"]["id"] == "page_001"
    finally:
        release.set()
        await asyncio.to_thread(watcher.join, 10)
    assert not watcher.is_alive()
    assert observed["pulse_during_io"] is True, (
        "Page HTTP read blocked aiohttp's event loop, preventing an otherwise "
        "ready durable Work lease heartbeat from running during slow I/O"
    )
