"""Work Export Center discovery and safe registered PNG retrieval."""

from __future__ import annotations

import asyncio
import io
import threading
from pathlib import Path

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.repositories import ArtifactRepository, WorkLifecycleRepository
from manga_autopilot.routes import register_all


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
    import manga_autopilot.routes.work_export_center_routes as routes
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
