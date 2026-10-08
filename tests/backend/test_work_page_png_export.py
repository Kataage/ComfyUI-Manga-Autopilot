"""Issue #240: saved Page Editor Layout -> selected Work Artifact -> PNG export."""

from __future__ import annotations

import asyncio
import hashlib
import io
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


def _png(color: tuple[int, int, int]) -> bytes:
    with io.BytesIO() as buffer:
        Image.new("RGB", (12, 12), color).save(buffer, format="PNG")
        return buffer.getvalue()


@pytest.fixture()
async def api(aiohttp_client, tmp_path: Path):
    handle = WorkLifecycleRepository(tmp_path).create_work(
        work_id="work_export_1", title="Saved layout export",
    )
    pages = PageRepository(handle.database_path)
    layouts = LayoutRepository(handle.database_path)
    panels = PanelRepository(handle.database_path)
    artifacts = ArtifactRepository(tmp_path, handle.work_id)
    pages.create_page(
        page_id="page_main", page_number=1, order_key="0001",
        page_purpose="Output persisted image",
    )
    pages.create_page(
        page_id="page_other", page_number=2, order_key="0002",
        page_purpose="Other",
    )
    layouts.create_layout(
        layout_id="layout_main", page_id="page_main",
        geometry={"width": 300, "height": 200},
    )
    layouts.create_layout(
        layout_id="layout_other", page_id="page_other",
        geometry={"width": 300, "height": 200},
    )
    layouts.create_slot(
        slot_id="slot_main", layout_id="layout_main", slot_key="top",
        reading_order=1,
        geometry={"x": 5, "y": 15, "width": 100, "height": 90},
    )
    layouts.create_slot(
        slot_id="slot_other", layout_id="layout_other", slot_key="top",
        reading_order=1,
        geometry={"x": 5, "y": 15, "width": 100, "height": 90},
    )
    panels.create_panel(
        panel_id="panel_main", page_id="page_main", order_index=1,
        panel_purpose="A vivid frame", layout_slot_id="slot_main",
        action={"character_id": "hero"},
    )
    panels.create_panel(
        panel_id="panel_other", page_id="page_other", order_index=1,
        panel_purpose="Other frame", layout_slot_id="slot_other",
    )
    artifacts.register_local_bytes(
        artifact_id="candidate_main",
        data=_png((235, 15, 25)), artifact_type="panel_candidate",
        scope_type="panel", scope_id="panel_main",
        mime_type="image/png",
        relative_path="assets/panels/candidate_main.png",
        dependency_fingerprint="hero_v1",
    )
    artifacts.register_local_bytes(
        artifact_id="candidate_other",
        data=_png((10, 20, 240)), artifact_type="panel_candidate",
        scope_type="panel", scope_id="panel_other",
        mime_type="image/png",
        relative_path="assets/panels/candidate_other.png",
        dependency_fingerprint="other_v1",
    )
    current = panels.get_panel("panel_main")
    panels.update_panel(
        "panel_main", expected_revision=current["revision"],
        selected_candidate_id="candidate_main",
    )
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    client = await aiohttp_client(app)
    base = "/manga_autopilot/api/v2/works/work_export_1/pages/page_main"
    return client, base, tmp_path, handle, pages, layouts, panels, artifacts


async def _export(client, base, payload=None):
    response = await client.post(base + "/export/png", json=payload or {})
    return response, await response.json()


async def test_restart_then_export_uses_saved_layout_from_page_editor_api(api, aiohttp_client):
    client, base, storage, handle, _, _, _, artifacts = api
    # The persisted GET is exactly what Page Editor reads.
    get_response = await client.get(base)
    assert get_response.status == 200
    old_state = await get_response.json()
    # No client-supplied layout is submitted to the backend export route.
    save_response = await client.patch(
        base + "/layout",
        json={
            "expected_revision": old_state["layout"]["revision"],
            "geometry_json": {"width": 280, "height": 210},
            "slot_updates": [{
                "id": "slot_main",
                "expected_revision": old_state["slots"][0]["revision"],
                "geometry_json": {"x": 145, "y": 30, "width": 100, "height": 100},
            }],
        },
    )
    assert save_response.status == 200, await save_response.text()
    saved = await save_response.json()
    assert saved["layout"]["geometry_json"] == {"width": 280, "height": 210}
    assert saved["slots"][0]["geometry_json"]["x"] == 145

    # Reconstructed HTTP application uses the same Work DB, not old JSON files.
    restarted_app = web.Application()
    register_all(restarted_app, storage_root=str(storage))
    restarted = await aiohttp_client(restarted_app)
    response, result = await _export(restarted, base)
    assert response.status == 201, result
    assert result["work_id"] == "work_export_1"
    assert result["page_id"] == "page_main"
    assert result["images_composited"] == 1
    assert result["panels_drawn"] == 1
    assert result["width"] == 280
    assert result["height"] == 210
    row = artifacts.get(result["artifact_id"])
    assert row["relative_path"] == result["relative_path"]
    assert row["relative_path"].startswith("exports/pages/page_main_")
    assert row["artifact_type"] == "page_render"
    assert row["scope_type"] == "page"
    assert row["scope_id"] == "page_main"
    assert row["dependency_fingerprint"] == result["dependency_fingerprint"]
    assert row["mime_type"] == "image/png"
    assert row["status"] == "READY"
    artifact_file = handle.root / row["relative_path"]
    assert artifact_file.is_file()
    assert row["sha256"] == hashlib.sha256(artifact_file.read_bytes()).hexdigest()
    assert artifacts.verify_registered_file(result["artifact_id"]) == row
    with Image.open(artifact_file) as image:
        assert image.size == (280, 210)
        # Moved layout must be reflected in the renderer, not old slot coords.
        assert image.getpixel((190, 75))[0] > 175
        assert image.getpixel((190, 75))[1] < 80
        assert image.getpixel((50, 45)) == (255, 255, 255)
    assert (handle.root / "exports" / "pages").is_dir()
    assert not (storage / "projects" / "work_export_1").exists()


async def test_repeated_exports_have_distinct_immutable_registered_artifacts(api):
    client, base, _, handle, _, _, _, artifacts = api
    first, a = await _export(client, base)
    second, b = await _export(client, base)
    assert first.status == second.status == 201
    assert a["artifact_id"] != b["artifact_id"]
    assert a["relative_path"] != b["relative_path"]
    assert a["dependency_fingerprint"] == b["dependency_fingerprint"]
    assert (handle.root / a["relative_path"]).is_file()
    assert (handle.root / b["relative_path"]).is_file()
    assert len(artifacts.list_for_scope("page", "page_main")) == 2


async def test_selected_artifact_ownership_is_enforced_not_guessed(api):
    client, base, _, handle, _, _, panels, artifacts = api
    from manga_autopilot.repositories import PageDomainCandidateSelectionError
    from manga_autopilot.storage import repository_write

    current = panels.get_panel("panel_main")
    with pytest.raises(PageDomainCandidateSelectionError, match="owned by this Panel"):
        panels.update_panel(
            "panel_main", expected_revision=current["revision"],
            selected_candidate_id="candidate_other",
        )
    assert panels.get_panel("panel_main") == current

    # Defensive export test for a Work written by an older unguarded version.
    # Only test setup bypasses the public repository selection transaction.
    with repository_write(handle.database_path) as db:
        db.execute(
            "UPDATE panels SET selected_candidate_id = ? WHERE id = ?",
            ("candidate_other", "panel_main"),
        )
    response, body = await _export(client, base)
    assert response.status == 422
    assert body["error"] == "export_precondition_failed"
    assert "owned by this Panel" in body["message"]
    assert artifacts.list_for_scope("page", "page_main") == []
    export_dir = handle.root / "exports" / "pages"
    assert not export_dir.exists() or not list(export_dir.iterdir())


async def test_missing_selected_image_is_actionable_and_does_not_create_blank_page(api):
    client, base, _, handle, _, _, panels, artifacts = api
    from manga_autopilot.repositories import PageDomainCandidateSelectionError
    from manga_autopilot.storage import repository_write

    current = panels.get_panel("panel_main")
    with pytest.raises(PageDomainCandidateSelectionError, match="Candidate"):
        panels.update_panel(
            "panel_main", expected_revision=current["revision"],
            selected_candidate_id="candidate_missing",
        )
    assert panels.get_panel("panel_main") == current

    # Read-time guard remains essential for historical invalid Work data.
    with repository_write(handle.database_path) as db:
        db.execute(
            "UPDATE panels SET selected_candidate_id = ? WHERE id = ?",
            ("candidate_missing", "panel_main"),
        )
    response, body = await _export(client, base)
    assert response.status == 422
    assert "candidate_missing" in body["message"]
    assert "Register an Artifact" in body["message"]
    assert artifacts.list_for_scope("page", "page_main") == []


async def test_missing_or_corrupt_image_refused_before_output_registration(api):
    client, base, _, handle, _, _, _, artifacts = api
    input_path = handle.root / "assets/panels/candidate_main.png"
    input_path.unlink()
    response, body = await _export(client, base)
    assert response.status == 422
    assert "missing" in body["message"]
    assert artifacts.list_for_scope("page", "page_main") == []
    input_path.write_bytes(b"tampered")
    response, body = await _export(client, base)
    assert response.status == 422
    assert "corrupt" in body["message"]
    assert artifacts.list_for_scope("page", "page_main") == []


async def test_missing_layout_or_unbound_panel_fails_with_instruction(api):
    client, base, _, _, pages, _, panels, artifacts = api
    unbound_page = pages.create_page(
        page_id="page_no_layout", page_number=3, order_key="0003",
        page_purpose="No layout",
    )
    url = base.replace("page_main", unbound_page["id"])
    response, body = await _export(client, url)
    assert response.status == 422
    assert "no persisted Layout" in body["message"]
    panel = panels.get_panel("panel_main")
    panels.update_panel(
        "panel_main", expected_revision=panel["revision"], layout_slot_id=None
    )
    response, body = await _export(client, base)
    assert response.status == 422
    assert "Bind this Panel" in body["message"]
    assert artifacts.list_for_scope("page", "page_main") == []


async def test_ambiguous_unselected_candidates_require_explicit_selection(api):
    client, base, _, _, _, _, panels, artifacts = api
    panel = panels.get_panel("panel_main")
    panels.update_panel(
        "panel_main", expected_revision=panel["revision"],
        selected_candidate_id=None,
    )
    artifacts.register_local_bytes(
        artifact_id="candidate_alternative", data=_png((0, 220, 0)),
        artifact_type="panel_candidate", scope_type="panel", scope_id="panel_main",
        mime_type="image/png", relative_path="assets/panels/candidate_alternative.png",
        dependency_fingerprint="hero_v2",
    )
    response, body = await _export(client, base)
    assert response.status == 422
    assert "Select a candidate" in body["message"]
    assert "found 2" in body["message"]


async def test_single_ready_candidate_without_explicit_selection_is_allowed(api):
    client, base, _, _, _, _, panels, _ = api
    panel = panels.get_panel("panel_main")
    panels.update_panel(
        "panel_main", expected_revision=panel["revision"],
        selected_candidate_id=None,
    )
    response, body = await _export(client, base)
    assert response.status == 201, body
    assert body["images_composited"] == 1


@pytest.mark.parametrize("payload", [
    {"panels": [{"panel_id": "panel_main", "x": 1}]},
    {"layout": {"geometry_json": {"width": 12}}},
    {"page_width": 30},
    {"background": "white"},
    {"background": "#xyzxyz"},
    {"outer_border": "false"},
])
async def test_export_rejects_client_layout_json_and_invalid_settings(api, payload):
    client, base, _, _, _, _, _, artifacts = api
    response, body = await _export(client, base, payload)
    assert response.status in (400, 422)
    assert body["error"] in ("invalid_request", "export_precondition_failed")
    assert artifacts.list_for_scope("page", "page_main") == []


async def test_missing_page_and_work_return_404(api):
    client, base, _, _, _, _, _, _ = api
    response, body = await _export(client, base.replace("page_main", "unknown_page"))
    assert response.status == 404
    assert body["error"] == "not_found"
    response, body = await _export(client, base.replace("work_export_1", "unknown_work"))
    assert response.status == 404
    assert body["error"] == "not_found"


async def test_concurrent_page_edit_during_render_blocks_stale_registration(api, monkeypatch):
    client, base, _, handle, _, layouts, _, artifacts = api
    import manga_autopilot.services.work_page_export as export_module

    original = export_module.render_page_to_png

    def render_then_change(*args, **kwargs):
        rendered = original(*args, **kwargs)
        current = layouts.get_layout("layout_main")
        layouts.update_layout(
            "layout_main", expected_revision=current["revision"],
            geometry_json={"width": 320, "height": 200},
        )
        return rendered

    monkeypatch.setattr(export_module, "render_page_to_png", render_then_change)
    response, body = await _export(client, base)
    assert response.status == 409
    assert body["error"] == "page_changed"
    assert artifacts.list_for_scope("page", "page_main") == []
    export_dir = handle.root / "exports" / "pages"
    assert not export_dir.exists() or not list(export_dir.iterdir())


async def test_edit_after_post_render_check_cannot_commit_stale_png(api, monkeypatch):
    client, base, _, handle, _, layouts, _, artifacts = api
    from manga_autopilot.storage import repository_read

    original = ArtifactRepository._register_verified_page_render_file
    mutations = []

    def edit_before_artifact_copy(self, **kwargs):
        # This is AFTER the exporter's old final self.pages.get_page() check.
        if kwargs["page_id"] == "page_main":
            current = layouts.get_layout("layout_main")
            layouts.update_layout(
                "layout_main", expected_revision=current["revision"],
                geometry_json={"width": 320, "height": 200},
            )
            mutations.append("layout")
        return original(self, **kwargs)

    monkeypatch.setattr(
        ArtifactRepository, "_register_verified_page_render_file",
        edit_before_artifact_copy
    )
    response, body = await _export(client, base)
    assert response.status == 409, body
    assert body["error"] == "page_changed"
    assert mutations == ["layout"]
    assert artifacts.list_for_scope("page", "page_main") == []
    with repository_read(handle.database_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM commits WHERE operation_type = 'register_artifact'"
        ).fetchone()[0] == 2  # Only the two fixture candidate registrations.
        assert db.execute(
            "SELECT COUNT(*) FROM entity_revisions WHERE entity_type = 'artifact'"
        ).fetchone()[0] == 2
    # The immutable output file is allowed to remain an unregistered orphan,
    # never a READY record pointing to an outdated Page snapshot.
    assert len(list((handle.root / "exports" / "pages").glob("*.png"))) == 1


async def test_panel_switch_after_file_publication_rejects_stale_png(api, monkeypatch):
    client, base, _, handle, _, _, panels, artifacts = api
    import manga_autopilot.repositories.artifacts as artifacts_module

    original_publish = artifacts_module._publish_exclusive
    edits = []

    def publish_then_switch(temp, destination):
        original_publish(temp, destination)
        if "exports" in destination.parts:
            current = panels.get_panel("panel_main")
            panels.update_panel(
                "panel_main", expected_revision=current["revision"],
                selected_candidate_id=None,
            )
            edits.append("panel_selection")

    monkeypatch.setattr(
        artifacts_module, "_publish_exclusive", publish_then_switch
    )
    response, body = await _export(client, base)
    assert response.status == 409, body
    assert body["error"] == "page_changed"
    assert edits == ["panel_selection"]
    assert artifacts.list_for_scope("page", "page_main") == []
    assert len(list((handle.root / "exports" / "pages").glob("*.png"))) == 1

    # Retry uses the committed Panel state; orphan publication never claims
    # to have succeeded and cannot poison the next immutable registration.
    response, body = await _export(client, base)
    assert response.status == 201, body
    assert len(artifacts.list_for_scope("page", "page_main")) == 1
    assert artifacts.verify_registered_file(body["artifact_id"])
    assert len(list((handle.root / "exports" / "pages").glob("*.png"))) == 2


async def test_new_candidate_after_file_publication_invalidates_implicit_choice(
    api, monkeypatch,
):
    client, base, _, _, _, _, panels, artifacts = api
    import manga_autopilot.repositories.artifacts as artifacts_module

    current = panels.get_panel("panel_main")
    panels.update_panel(
        "panel_main", expected_revision=current["revision"],
        selected_candidate_id=None,
    )
    original_publish = artifacts_module._publish_exclusive
    added = []

    def publish_then_add_candidate(temp, destination):
        original_publish(temp, destination)
        if "exports" in destination.parts:
            artifacts.register_local_bytes(
                artifact_id="candidate_late",
                data=_png((0, 220, 0)), artifact_type="panel_candidate",
                scope_type="panel", scope_id="panel_main",
                mime_type="image/png",
                relative_path="assets/panels/candidate_late.png",
                dependency_fingerprint="late_v1",
            )
            added.append(True)

    monkeypatch.setattr(
        artifacts_module, "_publish_exclusive", publish_then_add_candidate
    )
    response, body = await _export(client, base)
    assert response.status == 409, body
    assert body["error"] == "page_changed"
    assert added == [True]
    assert artifacts.list_for_scope("page", "page_main") == []
    assert artifacts.get("candidate_late")["status"] == "READY"


async def test_oversized_output_area_rejected_before_renderer_or_artifact(api, monkeypatch):
    client, base, _, handle, _, layouts, _, artifacts = api
    import manga_autopilot.services.work_page_export as module

    invoked = []
    monkeypatch.setattr(module, "render_page_to_png", lambda *a, **kw: invoked.append(kw))
    current = layouts.get_layout("layout_main")
    layouts.update_layout(
        "layout_main", expected_revision=current["revision"],
        geometry_json={"width": 5000, "height": 5000},
    )
    response, data = await _export(client, base)
    assert response.status == 422
    assert data["error"] == "export_precondition_failed"
    assert "25,000,000 pixels" in data["message"]
    assert "12,000,000" in data["message"]
    assert "Page page_main" in data["message"]
    assert "print" in data["message"]
    assert invoked == []
    assert artifacts.list_for_scope("page", "page_main") == []
    # Artifact fixture setup already creates assets/temp; reject renders must
    # not create or leave any transient page-export render directory.
    assert not list((handle.root / "assets" / "temp").glob("page-export-*"))


async def test_print_profile_allowed_to_reach_renderer_but_hard_caps_enforced(api, monkeypatch):
    client, base, _, _, _, layouts, _, artifacts = api
    import manga_autopilot.services.work_page_export as module

    current = layouts.get_layout("layout_main")
    layouts.update_layout(
        "layout_main", expected_revision=current["revision"],
        geometry_json={"width": 5000, "height": 4000},
    )
    called = []

    def stop_renderer(*args, **kwargs):
        called.append((kwargs["page_width"], kwargs["page_height"]))
        raise RuntimeError("renderer reached within named print profile")

    monkeypatch.setattr(module, "render_page_to_png", stop_renderer)
    response, data = await _export(client, base)
    assert response.status == 422
    assert "12,000,000" in data["message"]
    with pytest.raises(RuntimeError, match="renderer reached"):
        module.WorkPageExportService(base_storage(api)).export_png(
            "work_export_1", "page_main", export_profile="print"
        )
    assert called == [(5000, 4000)]
    assert artifacts.list_for_scope("page", "page_main") == []

    updated = layouts.get_layout("layout_main")
    layouts.update_layout(
        "layout_main", expected_revision=updated["revision"],
        geometry_json={"width": 6000, "height": 5000},
    )
    response, data = await _export(client, base, {"export_profile": "print"})
    assert response.status == 422
    assert "24,000,000 pixels" in data["message"]
    assert called == [(5000, 4000)]


def base_storage(api):
    return api[2]


@pytest.mark.parametrize("profile", ["ultra", "", None, 500, {}, []])
async def test_unknown_export_profile_is_rejected(api, profile):
    client, base, _, _, _, _, _, artifacts = api
    response, data = await _export(client, base, {"export_profile": profile})
    assert response.status == 422
    assert data["error"] == "export_precondition_failed"
    assert "export_profile" in data["message"]
    assert artifacts.list_for_scope("page", "page_main") == []


async def test_candidate_pixel_metadata_budget_rejected_before_decode(api, monkeypatch):
    client, base, _, _, _, _, _, artifacts = api
    import manga_autopilot.services.work_page_export as module
    from manga_autopilot.storage import repository_write

    with repository_write(artifacts._open().database_path) as db:
        db.execute(
            "UPDATE artifacts SET width = ?, height = ? WHERE id = ?",
            (5000, 5000, "candidate_main"),
        )
    invoked = []
    monkeypatch.setattr(module, "render_page_to_png", lambda *a, **kw: invoked.append(kw))
    response, data = await _export(client, base)
    assert response.status == 422
    assert "input pixel budget exceeded" in data["message"]
    assert invoked == []


async def test_generated_png_file_size_budget_before_artifact_registration(api, monkeypatch):
    client, base, _, handle, _, _, _, artifacts = api
    import manga_autopilot.services.work_page_export as module
    from manga_autopilot.services.page_png_budget import PROFILES
    from manga_autopilot.services.page_renderer import PageRenderResult

    def sparse_oversized_render(page_id, layouts, *, output_dir, **kwargs):
        path = Path(output_dir) / "page_0001.png"
        with path.open("wb") as output:
            output.truncate(PROFILES["screen"].max_png_bytes + 1)
        return PageRenderResult(
            page_id=page_id, output_path=path, width=300, height=200,
            panels_drawn=1, images_composited=1,
        )

    monkeypatch.setattr(module, "render_page_to_png", sparse_oversized_render)
    response, data = await _export(client, base)
    assert response.status == 422
    assert "rendered PNG" in data["message"]
    assert "bytes" in data["message"]
    assert artifacts.list_for_scope("page", "page_main") == []
    assert not (handle.root / "exports" / "pages").exists()


async def test_process_local_render_backpressure_does_not_start_second_canvas(api, monkeypatch):
    _, _, storage, _, _, _, _, artifacts = api
    import manga_autopilot.services.work_page_export as module

    original = module.render_page_to_png
    entered = threading.Event()
    release = threading.Event()

    def suspended_renderer(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=20)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "render_page_to_png", suspended_renderer)
    service = module.WorkPageExportService(storage)
    first = asyncio.create_task(
        asyncio.to_thread(service.export_png, "work_export_1", "page_main")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 12)
        with pytest.raises(module.PageExportBusyError, match="another PNG render"):
            module.WorkPageExportService(storage).export_png(
                "work_export_1", "page_main"
            )
    finally:
        release.set()
    done = await asyncio.wait_for(first, timeout=25)
    assert done["images_composited"] == 1
    assert len(artifacts.list_for_scope("page", "page_main")) == 1


async def test_archived_panel_is_not_rendered_and_historical_png_remains(
    api,
):
    client, base, _, handle, _, layouts, panels, artifacts = api
    # Two distinct Panel candidates render at separate saved Slots.
    layouts.create_slot(
        slot_id="slot_live", layout_id="layout_main", slot_key="right",
        reading_order=2, geometry={"x": 150, "y": 15, "width": 100, "height": 90},
    )
    panels.create_panel(
        panel_id="panel_live", page_id="page_main", order_index=2,
        panel_purpose="Still active", layout_slot_id="slot_live",
    )
    artifacts.register_local_bytes(
        artifact_id="candidate_live", data=_png((20, 40, 230)),
        relative_path="assets/panels/candidate_live.png",
        artifact_type="panel_candidate", scope_type="panel",
        scope_id="panel_live", mime_type="image/png",
        dependency_fingerprint="live-v1",
    )
    current = panels.get_panel("panel_live")
    panels.update_panel(
        "panel_live", expected_revision=current["revision"],
        selected_candidate_id="candidate_live",
    )
    first_response, first = await _export(client, base)
    assert first_response.status == 201, first
    assert first["images_composited"] == 2
    old_file = handle.root / first["relative_path"]
    old_bytes = old_file.read_bytes()
    old_record = artifacts.get(first["artifact_id"])

    # An archived Panel must not cause a new output to read its Candidate.
    archived = panels.get_panel("panel_main")
    panels.update_panel(
        "panel_main", expected_revision=archived["revision"],
        archived_at="2026-10-08T12:00:00Z",
    )
    (handle.root / "assets/panels/candidate_main.png").unlink()
    second_response, second = await _export(client, base)
    assert second_response.status == 201, second
    assert second["panels_drawn"] == 1
    assert second["images_composited"] == 1
    with Image.open(handle.root / second["relative_path"]) as rendered:
        assert rendered.getpixel((50, 50)) == (255, 255, 255)
        assert rendered.getpixel((190, 50))[2] > 180
    assert old_file.read_bytes() == old_bytes
    assert artifacts.get(first["artifact_id"]) == old_record
    all_exports = await client.get(
        "/manga_autopilot/api/v2/works/work_export_1/exports"
    )
    entries = (await all_exports.json())["exports"]
    assert [row["freshness"] for row in entries[:2]] == ["CURRENT", "STALE"]
    # The archived Panel and its immutable candidate rows are still present.
    assert panels.get_panel("panel_main")["archived_at"] is not None
    assert artifacts.get("candidate_main")["id"] == "candidate_main"

    panels.update_panel(
        "panel_main", expected_revision=archived["revision"] + 1,
        archived_at=None,
    )
    # Unarchiving is reversible; restore input file for the next output.
    (handle.root / "assets/panels/candidate_main.png").write_bytes(_png((235, 15, 25)))
    third_response, third = await _export(client, base)
    assert third_response.status == 201, third
    assert third["images_composited"] == 2


async def test_archived_page_and_all_panels_archived_refuse_active_export(api):
    client, base, _, handle, pages, _, panels, artifacts = api
    existing_response, existing = await _export(client, base)
    assert existing_response.status == 201
    preserved = (handle.root / existing["relative_path"]).read_bytes()
    page = pages.get_page("page_main")
    pages.update_page(
        "page_main", expected_revision=page["revision"],
        archived_at="2026-10-08T12:10:00Z",
    )
    blocked_response, blocked = await _export(client, base)
    assert blocked_response.status == 422
    assert blocked["error"] == "export_precondition_failed"
    assert "archived" in blocked["message"]
    assert len(artifacts.list_for_scope("page", "page_main")) == 1
    listed = await client.get("/manga_autopilot/api/v2/works/work_export_1/pages")
    assert [p["id"] for p in (await listed.json())["pages"]] == ["page_other"]
    recovery = await client.get(
        "/manga_autopilot/api/v2/works/work_export_1/pages/page_main"
        "?include_archived=1"
    )
    assert recovery.status == 200
    historical = await client.get(
        "/manga_autopilot/api/v2/works/work_export_1/exports/"
        + existing["artifact_id"] + "/png"
    )
    assert historical.status == 200
    assert await historical.read() == preserved

    pages.update_page(
        "page_main", expected_revision=page["revision"] + 1,
        archived_at=None,
    )
    panel = panels.get_panel("panel_main")
    panels.update_panel(
        "panel_main", expected_revision=panel["revision"],
        archived_at="2026-10-08T12:11:00Z",
    )
    no_panel_response, no_panel = await _export(client, base)
    assert no_panel_response.status == 422
    assert "no active Panels" in no_panel["message"]
    assert len(artifacts.list_for_scope("page", "page_main")) == 1
    panels.update_panel(
        "panel_main", expected_revision=panel["revision"] + 1,
        archived_at=None,
    )
    accepted, output = await _export(client, base)
    assert accepted.status == 201, output
    assert output["images_composited"] == 1


async def test_page_archive_after_file_publication_cannot_register_ready_png(
    api, monkeypatch,
):
    client, base, _, handle, pages, _, _, artifacts = api
    import manga_autopilot.repositories.artifacts as artifact_module

    original = artifact_module._publish_exclusive
    archived = []

    def publish_then_archive(temp, destination):
        original(temp, destination)
        if "exports" in destination.parts:
            current = pages.get_page("page_main")
            pages.update_page(
                "page_main", expected_revision=current["revision"],
                archived_at="2026-10-08T12:20:00Z",
            )
            archived.append(True)

    monkeypatch.setattr(artifact_module, "_publish_exclusive", publish_then_archive)
    response, body = await _export(client, base)
    assert response.status == 409, body
    assert body["error"] == "page_changed"
    assert archived == [True]
    assert artifacts.list_for_scope("page", "page_main") == []
    assert len(list((handle.root / "exports/pages").glob("*.png"))) == 1


async def test_http_page_render_does_not_block_other_requests_or_busy_reply(
    api, monkeypatch,
):
    client, base, _, handle, _, _, _, artifacts = api
    import manga_autopilot.services.work_page_export as module

    real_renderer = module.render_page_to_png
    started = threading.Event()
    release = threading.Event()
    calls = []

    def suspended_renderer(*args, **kwargs):
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=20), "test renderer release timed out"
        return real_renderer(*args, **kwargs)

    monkeypatch.setattr(module, "render_page_to_png", suspended_renderer)
    first = asyncio.create_task(_export(client, base))
    try:
        assert await asyncio.to_thread(started.wait, 10), (
            "the first real aiohttp POST never entered Pillow"
        )

        async def page_snapshot():
            response = await client.get(base)
            return response.status, await response.json()

        async def exports_snapshot():
            response = await client.get(
                "/manga_autopilot/api/v2/works/work_export_1/exports"
            )
            return response.status, await response.json()

        # These actual HTTP requests MUST complete while the first PNG POST
        # still owns the render slot. The old synchronous route blocks the
        # event loop and cannot make this gather finish before release.
        (page_status, page_body), (list_status, listed), (busy_response, busy) = (
            await asyncio.wait_for(
                asyncio.gather(
                    page_snapshot(), exports_snapshot(), _export(client, base),
                ),
                timeout=6,
            )
        )
        assert page_status == 200
        assert page_body["page"]["id"] == "page_main"
        assert list_status == 200
        assert listed["exports"] == []
        assert busy_response.status == 429
        assert busy_response.headers["Retry-After"] == "2"
        assert busy["error"] == "export_busy"
        assert len(calls) == 1, "the busy request started another Pillow canvas"
        assert not first.done(), "render completed before release"
    finally:
        release.set()

    first_response, first_png = await asyncio.wait_for(first, timeout=20)
    assert first_response.status == 201, first_png
    assert first_png["images_composited"] == 1
    assert len(calls) == 1
    row = artifacts.get(first_png["artifact_id"])
    assert row["status"] == "READY"
    assert artifacts.verify_registered_file(row["id"]) == row
    assert (handle.root / row["relative_path"]).is_file()

    # Subsequent clients can render again; only the in-progress work was busy.
    next_response, next_png = await _export(client, base)
    assert next_response.status == 201, next_png
    assert next_png["artifact_id"] != first_png["artifact_id"]
    assert len(calls) == 2

    gated = await client.get(
        "/manga_autopilot/api/v2/works/work_export_1/exports/"
        + first_png["artifact_id"] + "/png?require_current=1"
    )
    assert gated.status == 200
    assert gated.headers["X-Work-Export-Freshness"] == "CURRENT"
    assert await gated.read() == (handle.root / row["relative_path"]).read_bytes()


async def test_cancelling_offloaded_http_worker_cleans_temp_and_releases_slot(
    api, monkeypatch,
):
    _, _, root, handle, _, _, _, artifacts = api
    import manga_autopilot.services.work_page_export as module
    from manga_autopilot.routes.work_page_export_routes import _run_page_export

    original_renderer = module.render_page_to_png
    started = threading.Event()
    release = threading.Event()

    def suspended_renderer(*args, **kwargs):
        started.set()
        assert release.wait(timeout=20), "test renderer release timed out"
        return original_renderer(*args, **kwargs)

    monkeypatch.setattr(module, "render_page_to_png", suspended_renderer)
    service = module.WorkPageExportService(root)
    cancelled_http_task = asyncio.create_task(
        _run_page_export(service, "work_export_1", "page_main", {})
    )
    try:
        assert await asyncio.to_thread(started.wait, 10)
        cancelled_http_task.cancel()
        # Allow the route helper to signal cancellation to its worker before
        # releasing Pillow. A detached to_thread() call would leave a READY
        # Artifact behind even though its HTTP request was abandoned.
        await asyncio.sleep(0)
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cancelled_http_task, timeout=20)
    assert artifacts.list_for_scope("page", "page_main") == []
    assert not list((handle.root / "assets" / "temp").glob("page-export-*"))
    export_dir = handle.root / "exports" / "pages"
    assert not export_dir.exists() or not list(export_dir.glob("*.png"))

    # The render semaphore is released by the finished worker, not by the
    # cancelled asyncio waiter. A clean later POST can publish normally.
    result = await asyncio.to_thread(
        module.WorkPageExportService(root).export_png,
        "work_export_1", "page_main",
    )
    assert artifacts.verify_registered_file(result["artifact_id"])["status"] == "READY"


async def test_offloaded_http_renderer_error_releases_busy_slot(api, monkeypatch):
    client, base, _, _, _, _, _, artifacts = api
    import manga_autopilot.services.work_page_export as module

    original = module.render_page_to_png

    def broken_renderer(*args, **kwargs):
        raise RuntimeError("intentional Pillow error")

    monkeypatch.setattr(module, "render_page_to_png", broken_renderer)
    response = await client.post(base + "/export/png", json={})
    assert response.status == 500
    await response.read()
    assert artifacts.list_for_scope("page", "page_main") == []

    monkeypatch.setattr(module, "render_page_to_png", original)
    response, result = await _export(client, base)
    assert response.status == 201, result
