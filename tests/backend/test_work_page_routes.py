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
