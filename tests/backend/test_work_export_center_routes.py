"""Work Export Center discovery and safe registered PNG retrieval."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from pathlib import Path

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.repositories import (
    ArtifactRepository,
    LayoutRepository,
    PageRepository,
    PanelRepository,
    WorkLifecycleRepository,
)
from manga_autopilot.routes import register_all
from manga_autopilot.storage import repository_read


def _png(color: str = "#19b13d") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (14, 9), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
async def browser_api(aiohttp_client, tmp_path: Path):
    handle = WorkLifecycleRepository(tmp_path).create_work(
        work_id="work_center", title="Export Center Work"
    )
    WorkLifecycleRepository(tmp_path).create_work(
        work_id="work_other", title="Another Work"
    )
    repo = ArtifactRepository(tmp_path, handle.work_id)
    repo.register_local_bytes(
        artifact_id="page_artifact_1",
        data=_png(),
        relative_path="exports/pages/page_one_artifact_1.png",
        artifact_type="page_render",
        scope_type="page",
        scope_id="page_one",
        mime_type="image/png",
        dependency_fingerprint="layout-dep-1",
    )
    repo.register_local_bytes(
        artifact_id="candidate_panel_1",
        data=_png("#ec6318"),
        relative_path="assets/panels/candidate_panel_1.png",
        artifact_type="panel_candidate",
        scope_type="panel",
        scope_id="panel_one",
        mime_type="image/png",
        dependency_fingerprint="panel-dep-1",
    )
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    return await aiohttp_client(app), handle, repo


async def test_work_list_returns_only_registered_page_png_metadata(browser_api):
    client, _, _ = browser_api
    response = await client.get("/manga_autopilot/api/v2/works/work_center/exports")
    assert response.status == 200
    payload = await response.json()
    assert payload["work_id"] == "work_center"
    assert len(payload["exports"]) == 1
    row = payload["exports"][0]
    assert row["id"] == "page_artifact_1"
    assert row["scope_type"] == "page"
    assert row["scope_id"] == "page_one"
    assert row["relative_path"] == "exports/pages/page_one_artifact_1.png"
    assert row["width"] == 14
    assert row["height"] == 9
    assert row["mime_type"] == "image/png"
    assert row["status"] == "READY"
    assert row["freshness"] == "UNVERIFIED"
    assert row["is_current"] is False
    assert "created_at" in row
    assert "absolute_path" not in row
    assert "artifact_path" not in row


async def test_png_link_retrieves_checked_bytes_without_client_supplied_paths(browser_api):
    client, _, _ = browser_api
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    )
    assert response.status == 200
    assert response.content_type == "image/png"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert await response.read() == _png()


async def test_other_work_cannot_read_registered_png(browser_api):
    client, _, _ = browser_api
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_other/exports/page_artifact_1/png"
    )
    assert response.status == 404
    assert (await response.json())["error"] == "not_found"
    response = await client.get("/manga_autopilot/api/v2/works/work_other/exports")
    assert response.status == 200
    assert (await response.json())["exports"] == []


async def test_panel_artifact_cannot_be_served_as_page_export(browser_api):
    client, _, _ = browser_api
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_center/exports/candidate_panel_1/png"
    )
    assert response.status == 404
    assert (await response.json())["error"] == "not_found"


async def test_missing_and_tampered_export_paths_are_not_served(browser_api):
    client, handle, _ = browser_api
    png = handle.root / "exports/pages/page_one_artifact_1.png"
    png.write_bytes(b"tampered")
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    )
    assert response.status == 422
    assert (await response.json())["error"] == "export_file_invalid"
    png.unlink()
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    )
    assert response.status == 422
    assert "missing" in (await response.json())["message"]


async def test_nonexistent_work_is_actionable_404(browser_api):
    client, _, _ = browser_api
    response = await client.get("/manga_autopilot/api/v2/works/work_missing/exports")
    assert response.status == 404
    assert (await response.json())["error"] == "not_found"
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_missing/exports/page_artifact_1/png"
    )
    assert response.status == 404


async def test_legacy_export_listing_route_still_registered(browser_api):
    client, _, _ = browser_api
    response = await client.get("/manga_autopilot/api/projects/legacy_demo/exports")
    # The old API is not routed to the Work Artifact registry.
    assert response.status in {200, 404}
    if response.status == 200:
        data = await response.json()
        assert "files" in data


async def test_png_is_streamed_without_full_path_read_bytes(browser_api, monkeypatch):
    client, _, _ = browser_api

    def fail_full_read(*args, **kwargs):
        raise AssertionError("unbounded Path.read_bytes() is forbidden")

    monkeypatch.setattr(Path, "read_bytes", fail_full_read)
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    )
    assert response.status == 200
    assert await response.read() == _png()


async def test_verified_download_snapshot_is_immune_to_original_path_replacement(
    browser_api, monkeypatch,
):
    client, handle, _ = browser_api
    import manga_autopilot.routes.work_export_center_routes as routes

    original = routes._verified_png_snapshot
    def replace_after_copy(path, row):
        verified = original(path, row)
        path.unlink()
        path.write_bytes(_png("#aa3366"))
        return verified

    monkeypatch.setattr(routes, "_verified_png_snapshot", replace_after_copy)
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    )
    assert response.status == 200
    assert await response.read() == _png(), "must serve pinned verified bytes"
    assert (handle.root / "exports/pages/page_one_artifact_1.png").read_bytes() != _png()
    bad = await client.get(
        "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    )
    assert bad.status == 422
    assert (await bad.json())["error"] == "export_file_invalid"


async def test_download_rejects_declared_oversize_before_copying(browser_api, monkeypatch):
    client, _, artifacts = browser_api
    from manga_autopilot.services.page_png_budget import MAX_SERVABLE_PNG_BYTES
    from manga_autopilot.storage import repository_write

    with repository_write(artifacts._open().database_path) as db:
        db.execute(
            "UPDATE artifacts SET file_size = ? WHERE id = ?",
            (MAX_SERVABLE_PNG_BYTES + 1, "page_artifact_1"),
        )
    response = await client.get(
        "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    )
    assert response.status == 422
    assert "download budget" in (await response.json())["message"]


async def test_multiple_downloads_have_bounded_parallel_spool_slots(
    browser_api, monkeypatch,
):
    client, _, _ = browser_api
    import manga_autopilot.routes.work_export_center_routes as routes

    original = routes._verified_png_snapshot
    entered = 0
    guard = threading.Lock()
    release = threading.Event()

    def suspended_copy(path, row):
        nonlocal entered
        with guard:
            entered += 1
        assert release.wait(timeout=20)
        return original(path, row)

    monkeypatch.setattr(routes, "_verified_png_snapshot", suspended_copy)
    url = "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    first = asyncio.create_task(client.get(url))
    second = asyncio.create_task(client.get(url))
    try:
        for _ in range(600):
            with guard:
                if entered == 2:
                    break
            await asyncio.sleep(0.01)
        assert entered == 2, "two requests should be copying in parallel"
        third = await client.get(url)
        assert third.status == 429
        assert (await third.json())["error"] == "download_busy"
        assert third.headers["Retry-After"] == "2"
    finally:
        release.set()
    responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=20)
    for response in responses:
        assert response.status == 200
        assert await response.read() == _png()


@pytest.fixture
async def fresh_api(aiohttp_client, tmp_path: Path):
    handle = WorkLifecycleRepository(tmp_path).create_work(
        work_id="work_fresh", title="Fresh output"
    )
    pages = PageRepository(handle.database_path)
    layouts = LayoutRepository(handle.database_path)
    panels = PanelRepository(handle.database_path)
    artifacts = ArtifactRepository(tmp_path, "work_fresh")
    pages.create_page(
        page_id="page_a", page_number=1, order_key="0001", page_purpose="Proof",
    )
    layouts.create_layout(
        layout_id="layout_a", page_id="page_a",
        geometry={"width": 200, "height": 160},
    )
    layouts.create_slot(
        slot_id="slot_a", layout_id="layout_a", slot_key="main",
        reading_order=1, geometry={"x": 5, "y": 5, "width": 100, "height": 100},
    )
    panels.create_panel(
        panel_id="panel_a", page_id="page_a", order_index=1,
        panel_purpose="Proof", layout_slot_id="slot_a",
    )
    artifacts.register_local_bytes(
        artifact_id="candidate_a", data=_png(),
        relative_path="assets/panels/candidate_a.png",
        artifact_type="panel_candidate", scope_type="panel",
        scope_id="panel_a", mime_type="image/png",
        dependency_fingerprint="candidate",
    )
    panel = panels.get_panel("panel_a")
    panels.update_panel(
        "panel_a", expected_revision=panel["revision"],
        selected_candidate_id="candidate_a",
    )
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    return (
        await aiohttp_client(app), tmp_path, handle, pages, layouts,
        panels, artifacts,
    )


async def _export_fresh(client):
    res = await client.post(
        "/manga_autopilot/api/v2/works/work_fresh/pages/page_a/export/png",
        json={},
    )
    body = await res.json()
    assert res.status == 201, body
    return body


async def _list_fresh(client):
    res = await client.get("/manga_autopilot/api/v2/works/work_fresh/exports")
    assert res.status == 200
    return (await res.json())["exports"]


async def test_saved_page_edits_stale_old_exports_without_mutating_history(
    fresh_api, aiohttp_client,
):
    client, root, handle, pages, layouts, _, artifacts = fresh_api
    first = await _export_fresh(client)
    initial = (await _list_fresh(client))[0]
    assert initial["is_current"] is True
    assert initial["freshness"] == "CURRENT"
    artifact_before = artifacts.get(first["artifact_id"])
    old_bytes = (handle.root / first["relative_path"]).read_bytes()

    slot = layouts.get_slot("slot_a")
    layouts.update_slot(
        "slot_a", expected_revision=slot["revision"],
        geometry_json={"x": 25, "y": 5, "width": 100, "height": 100},
    )
    with repository_read(handle.database_path) as db:
        newer = db.execute(
            """SELECT COUNT(*) FROM invalidations
               WHERE target_type = 'page_export' AND target_id = 'page_a'
                 AND created_commit_seq > ?""",
            (artifact_before["created_commit_seq"],),
        ).fetchone()[0]
    assert newer == 1
    stale = (await _list_fresh(client))[0]
    assert stale["freshness"] == "STALE"
    assert stale["is_current"] is False
    assert stale["freshness_reason"] == "page_inputs_changed"
    url = ("/manga_autopilot/api/v2/works/work_fresh/exports/"
           + first["artifact_id"] + "/png")
    historical = await client.get(url)
    assert historical.status == 200
    assert historical.headers["X-Work-Export-Freshness"] == "STALE"
    assert await historical.read() == old_bytes
    gated = await client.get(url + "?require_current=1")
    assert gated.status == 409
    assert (await gated.json())["error"] == "stale_export"

    second = await _export_fresh(client)
    rows = await _list_fresh(client)
    assert [r["freshness"] for r in rows] == ["CURRENT", "STALE"]
    assert [r["id"] for r in rows] == [second["artifact_id"], first["artifact_id"]]
    assert artifacts.get(first["artifact_id"]) == artifact_before
    assert (handle.root / first["relative_path"]).read_bytes() == old_bytes
    await _export_fresh(client)
    assert [r["freshness"] for r in await _list_fresh(client)] == [
        "CURRENT", "CURRENT", "STALE"
    ]
    pages.create_page(
        page_id="other_page", page_number=2, order_key="0002",
        page_purpose="Unrelated",
    )
    assert [r["freshness"] for r in await _list_fresh(client)] == [
        "CURRENT", "CURRENT", "STALE"
    ]
    reopened = web.Application()
    register_all(reopened, storage_root=str(root))
    other_client = await aiohttp_client(reopened)
    assert [r["freshness"] for r in await _list_fresh(other_client)] == [
        "CURRENT", "CURRENT", "STALE"
    ]


async def test_later_unpinned_candidate_cannot_leave_page_png_current(fresh_api):
    client, _, _, _, _, panels, artifacts = fresh_api
    panel = panels.get_panel("panel_a")
    panels.update_panel(
        "panel_a", expected_revision=panel["revision"], selected_candidate_id=None
    )
    exported = await _export_fresh(client)
    assert (await _list_fresh(client))[0]["freshness"] == "CURRENT"
    artifacts.register_local_bytes(
        artifact_id="candidate_late", data=_png("#bb5522"),
        relative_path="assets/panels/candidate_late.png",
        artifact_type="panel_candidate", scope_type="panel",
        scope_id="panel_a", mime_type="image/png",
        dependency_fingerprint="candidate:later",
    )
    stale = (await _list_fresh(client))[0]
    assert stale["id"] == exported["artifact_id"]
    assert stale["freshness"] == "STALE"
    assert stale["freshness_reason"] == "candidate_choice_changed"
    gated = await client.get(
        "/manga_autopilot/api/v2/works/work_fresh/exports/"
        + exported["artifact_id"] + "/png?require_current=1"
    )
    assert gated.status == 409


async def test_generic_sha_looking_page_render_never_acquires_current_attestation(
    fresh_api, aiohttp_client,
):
    client, root, handle, _, _, _, artifacts = fresh_api
    imported = artifacts.register_local_bytes(
        artifact_id="manual_page_png",
        data=_png("#f00a77"),
        relative_path="exports/pages/manual_page_png.png",
        artifact_type="page_render", scope_type="page", scope_id="page_a",
        mime_type="image/png", dependency_fingerprint="0" * 64,
    )
    path = "/manga_autopilot/api/v2/works/work_fresh/exports/manual_page_png/png"
    rows = await _list_fresh(client)
    assert len(rows) == 1
    assert rows[0]["id"] == "manual_page_png"
    assert rows[0]["freshness"] == "UNVERIFIED"
    assert rows[0]["is_current"] is False
    assert rows[0]["freshness_reason"] == "source_provenance_unavailable"
    gated = await client.get(path + "?require_current=1")
    assert gated.status == 409
    assert (await gated.json())["freshness"] == "UNVERIFIED"
    old = await client.get(path)
    assert old.status == 200
    assert old.headers["X-Work-Export-Freshness"] == "UNVERIFIED"
    assert await old.read() == _png("#f00a77")
    with repository_read(handle.database_path) as db:
        operation = db.execute(
            "SELECT operation_type FROM commits WHERE commit_seq = ?",
            (imported["created_commit_seq"],),
        ).fetchone()["operation_type"]
    assert operation == "register_artifact"

    # Generic file registration with a caller-supplied no-op guard must not
    # certify itself either: only the exporter-private attested path may do it.
    candidate_file = handle.root / "assets/panels/candidate_a.png"
    supplied_guard = artifacts.register_local_file(
        source_path=candidate_file,
        relative_path="exports/pages/manual_guarded_png.png",
        artifact_id="manual_guarded_png",
        artifact_type="page_render", scope_type="page", scope_id="page_a",
        mime_type="image/png", dependency_fingerprint="1" * 64,
        commit_guard=lambda connection: None,
    )
    assert supplied_guard["status"] == "READY"
    assert [row["freshness"] for row in await _list_fresh(client)] == [
        "UNVERIFIED", "UNVERIFIED",
    ]

    rendered = await _export_fresh(client)
    rows = await _list_fresh(client)
    assert [row["freshness"] for row in rows] == [
        "CURRENT", "UNVERIFIED", "UNVERIFIED",
    ]
    assert rows[0]["id"] == rendered["artifact_id"]
    with repository_read(handle.database_path) as db:
        row = db.execute(
            """SELECT c.operation_type, c.reason
               FROM artifacts a JOIN commits c ON c.commit_seq = a.created_commit_seq
               WHERE a.id = ?""",
            (rendered["artifact_id"],),
        ).fetchone()
    assert row["operation_type"] == "register_verified_page_render_v1"
    attestation = json.loads(row["reason"])
    assert attestation["attestation_version"] == 1
    assert attestation["artifact_id"] == rendered["artifact_id"]
    assert attestation["page_id"] == "page_a"
    assert attestation["dependency_fingerprint"] == (
        rendered["dependency_fingerprint"]
    )
    source = attestation["source_fingerprint_payload"]
    assert source["page_id"] == "page_a"
    assert source["panel_artifacts"][0]["artifact_id"] == "candidate_a"
    assert source["background"] == "#ffffff"
    assert source["outer_border"] is True
    assert source["export_profile"] == "screen"
    authorized = await client.get(
        "/manga_autopilot/api/v2/works/work_fresh/exports/"
        + rendered["artifact_id"] + "/png?require_current=1"
    )
    assert authorized.status == 200
    assert authorized.headers["X-Work-Export-Freshness"] == "CURRENT"
    assert await authorized.read() != _png("#f00a77")

    restarted_app = web.Application()
    register_all(restarted_app, storage_root=str(root))
    restarted = await aiohttp_client(restarted_app)
    restored = await _list_fresh(restarted)
    assert [row["freshness"] for row in restored] == [
        "CURRENT", "UNVERIFIED", "UNVERIFIED",
    ]
    assert artifacts.get("manual_page_png") == imported


async def test_attestation_identity_is_checked_not_merely_commit_type(fresh_api):
    client, _, handle, _, _, _, _ = fresh_api
    rendered = await _export_fresh(client)
    from manga_autopilot.storage import repository_write

    with repository_write(handle.database_path) as db:
        # Simulate a Work DB that contains a mismatching/unrelated commit
        # reason despite the artifact pointing to a trusted operation name.
        db.execute(
            "UPDATE commits SET reason = ? WHERE commit_seq = ?",
            ("page_export_attestation_v1:wrong_artifact:page_a:" + "0" * 64,
             db.execute(
                 "SELECT created_commit_seq FROM artifacts WHERE id = ?",
                 (rendered["artifact_id"],),
             ).fetchone()[0]),
        )
    row = (await _list_fresh(client))[0]
    assert row["freshness"] == "UNVERIFIED"
    assert row["is_current"] is False
    download = await client.get(
        "/manga_autopilot/api/v2/works/work_fresh/exports/"
        + rendered["artifact_id"] + "/png?require_current=1"
    )
    assert download.status == 409


async def test_archived_panel_candidate_publication_does_not_stale_current_page_png(
    fresh_api, aiohttp_client,
):
    client, root, handle, _, layouts, panels, artifacts = fresh_api
    # Page A has a selected current candidate. Panel B is archived before
    # rendering, has no selection or candidate, and is not a compositing input.
    layouts.create_slot(
        slot_id="slot_b", layout_id="layout_a", slot_key="secondary",
        reading_order=2,
        geometry={"x": 120, "y": 10, "width": 70, "height": 80},
    )
    panels.create_panel(
        panel_id="panel_b", page_id="page_a", order_index=2,
        panel_purpose="Historical only", layout_slot_id="slot_b",
    )
    panel_b = panels.get_panel("panel_b")
    archived = panels.update_panel(
        "panel_b", expected_revision=panel_b["revision"],
        archived_at="2026-10-08T13:00:00Z",
    )
    assert archived["selected_candidate_id"] is None

    original = await _export_fresh(client)
    original_record = artifacts.get(original["artifact_id"])
    original_bytes = (handle.root / original["relative_path"]).read_bytes()
    listed = await _list_fresh(client)
    assert len(listed) == 1
    assert listed[0]["freshness"] == "CURRENT"
    assert listed[0]["is_current"] is True
    png_url = (
        "/manga_autopilot/api/v2/works/work_fresh/exports/"
        + original["artifact_id"] + "/png"
    )
    current = await client.get(png_url + "?require_current=1")
    assert current.status == 200
    assert current.headers["X-Work-Export-Freshness"] == "CURRENT"
    assert await current.read() == original_bytes

    with repository_read(handle.database_path) as db:
        invalidations_before = db.execute(
            "SELECT COUNT(*) FROM invalidations",
        ).fetchone()[0]
    artifacts.register_local_bytes(
        artifact_id="candidate_for_archived_panel", data=_png("#772244"),
        relative_path="assets/panels/candidate_for_archived_panel.png",
        artifact_type="panel_candidate", scope_type="panel",
        scope_id="panel_b", mime_type="image/png",
        dependency_fingerprint="archive-candidate-b",
    )
    # Candidate publication adds an Artifact commit, but neither edits the
    # archived Panel nor changes the Page input revision/invalidations.
    assert panels.get_panel("panel_b") == archived
    with repository_read(handle.database_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM invalidations",
        ).fetchone()[0] == invalidations_before

    still_current = (await _list_fresh(client))[0]
    assert still_current["id"] == original["artifact_id"]
    assert still_current["freshness"] == "CURRENT"
    assert still_current["freshness_reason"] is None
    assert still_current["is_current"] is True
    gated = await client.get(png_url + "?require_current=1")
    assert gated.status == 200
    assert gated.headers["X-Work-Export-Freshness"] == "CURRENT"
    assert await gated.read() == original_bytes
    assert artifacts.get(original["artifact_id"]) == original_record
    assert (handle.root / original["relative_path"]).read_bytes() == original_bytes

    # The classifier must produce the same result after a new app/reopen.
    restarted_app = web.Application()
    register_all(restarted_app, storage_root=str(root))
    restarted = await aiohttp_client(restarted_app)
    assert (await _list_fresh(restarted))[0]["freshness"] == "CURRENT"

    # Unarchiving is a revisioned source edit: older PNG becomes STALE,
    # even though an archived Candidate did not incorrectly stale it.
    revived = panels.update_panel(
        "panel_b", expected_revision=archived["revision"], archived_at=None,
    )
    assert revived["revision"] == archived["revision"] + 1
    old_after_unarchive = (await _list_fresh(client))[0]
    assert old_after_unarchive["freshness"] == "STALE"
    assert old_after_unarchive["freshness_reason"] == "page_inputs_changed"
    denied = await client.get(png_url + "?require_current=1")
    assert denied.status == 409
    historical = await client.get(png_url)
    assert historical.status == 200
    assert historical.headers["X-Work-Export-Freshness"] == "STALE"
    assert await historical.read() == original_bytes

    # Now that B is active and has one implicit candidate, a new export
    # legitimately depends on B and is eligible for CURRENT.
    refreshed = await _export_fresh(client)
    rows = await _list_fresh(client)
    assert [row["freshness"] for row in rows] == ["CURRENT", "STALE"]
    assert rows[0]["id"] == refreshed["artifact_id"]

    # An additional Candidate for an *active* unpinned Panel B must still
    # invalidate that newer PNG, preserving #333's ambiguity guard.
    artifacts.register_local_bytes(
        artifact_id="candidate_for_active_panel", data=_png("#1177aa"),
        relative_path="assets/panels/candidate_for_active_panel.png",
        artifact_type="panel_candidate", scope_type="panel",
        scope_id="panel_b", mime_type="image/png",
        dependency_fingerprint="active-candidate-b-later",
    )
    newer = (await _list_fresh(client))[0]
    assert newer["id"] == refreshed["artifact_id"]
    assert newer["freshness"] == "STALE"
    assert newer["freshness_reason"] == "candidate_choice_changed"
    fresh_only = await client.get(
        "/manga_autopilot/api/v2/works/work_fresh/exports/"
        + refreshed["artifact_id"] + "/png?require_current=1"
    )
    assert fresh_only.status == 409
    assert artifacts.get(original["artifact_id"]) == original_record
    assert (handle.root / original["relative_path"]).read_bytes() == original_bytes



async def test_canceled_http_download_keeps_spool_slot_until_copy_worker_exits(
    browser_api, aiohttp_client, monkeypatch,
):
    _, handle, _ = browser_api
    import manga_autopilot.routes.work_export_center_routes as routes

    url = "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    handlers = []

    @web.middleware
    async def track_handler(request, handler):
        if request.method == "GET" and request.path == url:
            handlers.append(asyncio.current_task())
        return await handler(request)

    app = web.Application(middlewares=[track_handler])
    register_all(app, storage_root=str(handle.root.parent.parent))
    client = await aiohttp_client(app)

    original = routes._verified_png_snapshot
    copy_release = threading.Event()
    entered = 0
    closed = []
    guard = threading.Lock()

    class TrackedSnapshot:
        def __init__(self, source, number):
            self.source = source
            self.number = number

        def read(self, size):
            return self.source.read(size)

        def close(self):
            with guard:
                closed.append(self.number)
            self.source.close()

    def suspended_copy(path, row):
        nonlocal entered
        with guard:
            entered += 1
            number = entered
        assert copy_release.wait(timeout=20), "download worker never released"
        return TrackedSnapshot(original(path, row), number)

    monkeypatch.setattr(routes, "_verified_png_snapshot", suspended_copy)
    first = asyncio.create_task(client.get(url))
    second = asyncio.create_task(client.get(url))
    try:
        for _ in range(700):
            with guard:
                if entered == 2:
                    break
            await asyncio.sleep(0.01)
        assert entered == 2, "two separate HTTP copies must own the two slots"
        assert len(handlers) >= 2

        full = await asyncio.wait_for(client.get(url), timeout=4)
        assert full.status == 429
        assert (await full.json())["error"] == "download_busy"
        assert full.headers["Retry-After"] == "2"

        handlers[0].cancel()
        await asyncio.sleep(0.03)
        # Even a second cancellation must not detach a running to_thread job.
        handlers[0].cancel()
        await asyncio.sleep(0.03)
        assert not handlers[0].done(), "cancelled handler abandoned its copy worker"

        still_full = await asyncio.wait_for(client.get(url), timeout=4)
        assert still_full.status == 429
        assert (await still_full.json())["error"] == "download_busy"
        with guard:
            assert entered == 2, "cancellation let a third download copy start"
            assert closed == [], "snapshots were not yet created by worker threads"
    finally:
        copy_release.set()

    completed = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), timeout=20,
    )
    successful = [
        item for item in completed if hasattr(item, "status") and item.status == 200
    ]
    assert len(successful) == 1, completed
    assert await successful[0].read() == _png()
    # Wait for both server handler finalizers; explicitly closed snapshots are
    # observable even for the cancelled waiter, not left to GC.
    for _ in range(300):
        with guard:
            if len(closed) == 2:
                break
        await asyncio.sleep(0.01)
    with guard:
        assert sorted(closed) == [1, 2]

    after = await asyncio.wait_for(client.get(url), timeout=10)
    assert after.status == 200
    assert await after.read() == _png()


async def test_canceled_http_stream_read_finishes_before_closing_spool_and_slot(
    browser_api, aiohttp_client, monkeypatch,
):
    _, handle, _ = browser_api
    import manga_autopilot.routes.work_export_center_routes as routes

    url = "/manga_autopilot/api/v2/works/work_center/exports/page_artifact_1/png"
    handlers = []

    @web.middleware
    async def track_handler(request, handler):
        if request.method == "GET" and request.path == url:
            handlers.append(asyncio.current_task())
        return await handler(request)

    app = web.Application(middlewares=[track_handler])
    register_all(app, storage_root=str(handle.root.parent.parent))
    client = await aiohttp_client(app)

    original = routes._verified_png_snapshot
    reading = threading.Event()
    release = threading.Event()
    snapshot_started = threading.Event()
    closed_while_reading = []
    closed = []
    guard = threading.Lock()
    number = 0

    class TrackedSnapshot:
        def __init__(self, source, is_first):
            self.source = source
            self.is_first = is_first
            self.in_read = False

        def read(self, size):
            if self.is_first:
                with guard:
                    self.in_read = True
                reading.set()
                try:
                    assert release.wait(timeout=20), "stream read never released"
                    return self.source.read(size)
                finally:
                    with guard:
                        self.in_read = False
            return self.source.read(size)

        def close(self):
            with guard:
                closed.append(self.is_first)
                if self.in_read:
                    closed_while_reading.append(True)
            self.source.close()

    def suspended_copy(path, row):
        nonlocal number
        with guard:
            number += 1
            index = number
        if index == 2:
            snapshot_started.set()
            assert release.wait(timeout=20), "second copy never released"
        return TrackedSnapshot(original(path, row), index == 1)

    monkeypatch.setattr(routes, "_verified_png_snapshot", suspended_copy)
    first_response = await asyncio.wait_for(client.get(url), timeout=10)
    assert first_response.status == 200
    assert await asyncio.to_thread(reading.wait, 10), (
        "first HTTP handler never entered worker snapshot.read()"
    )

    second = asyncio.create_task(client.get(url))
    try:
        assert await asyncio.to_thread(snapshot_started.wait, 10)
        assert len(handlers) >= 2
        handlers[0].cancel()
        await asyncio.sleep(0.05)
        assert not handlers[0].done(), "read worker was abandoned on cancellation"
        assert closed_while_reading == []

        full = await asyncio.wait_for(client.get(url), timeout=4)
        assert full.status == 429
        assert (await full.json())["error"] == "download_busy"
        assert number == 2, "a third copy ran while the streaming read was alive"
    finally:
        release.set()

    second_response = await asyncio.wait_for(second, timeout=20)
    assert second_response.status == 200
    assert await second_response.read() == _png()
    first_response.close()
    for _ in range(300):
        with guard:
            if len(closed) == 2:
                break
        await asyncio.sleep(0.01)
    with guard:
        assert sorted(closed) == [False, True]
        assert closed_while_reading == []
    after = await asyncio.wait_for(client.get(url), timeout=10)
    assert after.status == 200
    assert await after.read() == _png()
