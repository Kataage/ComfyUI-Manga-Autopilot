"""Issue #242: GPU-free real-HTTP vertical integration through actual browser JS.

The Node subprocess mounts production Page Editor and Export Center modules in
a small DOM-event shim. Their requests hit an *actual* aiohttp application and
Work SQLite database (not mocked fetch). A second fresh Node process and a
restarted HTTP server prove reopening from Work-owned state, not client JSON.

This test does not claim graphical Chromium/ComfyUI UI automation.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, web
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

ROOT = Path(__file__).resolve().parents[2]
BROWSER = ROOT / "tests" / "frontend" / "work_vertical_slice_live.mjs"
WORK_ID = "work_vertical_242"
PAGE_ID = "page_1"


def _png(rgb: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), rgb).save(buffer, format="PNG")
    return buffer.getvalue()


def _assert_export_file(
    work_root: Path,
    artifacts: ArtifactRepository,
    record: dict[str, Any],
    *,
    width: int,
    red_xy: tuple[int, int],
    white_xy: tuple[int, int],
) -> None:
    row = artifacts.get(record["artifact_id"])
    assert row["scope_type"] == "page"
    assert row["scope_id"] == PAGE_ID
    assert row["artifact_type"] == "page_render"
    assert row["status"] == "READY"
    assert row["mime_type"] == "image/png"
    assert row["width"] == width
    assert row["height"] == 200
    assert row["relative_path"] == record["relative_path"]
    assert row["dependency_fingerprint"] == record["dependency_fingerprint"]
    assert row["sha256"] == record["sha256"]
    assert row["file_size"] == record["file_size"]
    assert row["relative_path"].startswith("exports/pages/")
    assert artifacts.verify_registered_file(row["id"]) == row
    path = work_root / row["relative_path"]
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == record["sha256"]
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == (width, 200)
        red = image.getpixel(red_xy)
        white = image.getpixel(white_xy)
        blue = image.getpixel((220, 140))
    assert red[0] > 190 and red[1] < 70 and red[2] < 80
    assert white == (255, 255, 255)
    assert blue[2] > 185 and blue[0] < 80


async def _run_browser(
    node: str, phase: str, server_origin: str, preferences_path: Path,
) -> dict[str, Any]:
    """Allow the aiohttp event loop to serve real requests while Node runs."""
    process = await asyncio.create_subprocess_exec(
        node, str(BROWSER), phase, server_origin, str(preferences_path),
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        pytest.fail(f"live browser phase {phase} exceeded 90 seconds")
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    assert process.returncode == 0, (
        f"live browser phase {phase} failed (exit {process.returncode}):\n"
        f"stdout:\n{out}\nstderr:\n{err}"
    )
    return json.loads(out.strip().splitlines()[-1])


def _app(storage_root: Path) -> web.Application:
    app = web.Application()
    register_all(app, storage_root=str(storage_root))
    return app


async def test_actual_editor_ui_save_export_reopen_edit_export_again(
    tmp_path: Path, aiohttp_server,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for live v2 Work UI integration")
    assert BROWSER.is_file(), "native JS live-browser test must exist"

    lifecycle = WorkLifecycleRepository(tmp_path)
    handle = lifecycle.create_work(work_id=WORK_ID, title="Vertical v2 demo")
    lifecycle.create_work(work_id="work_unrelated", title="Isolation target")
    pages = PageRepository(handle.database_path)
    layouts = LayoutRepository(handle.database_path)
    panels = PanelRepository(handle.database_path)
    artifacts = ArtifactRepository(tmp_path, WORK_ID)

    # All v2 entities are persisted by production repositories, not by
    # injecting a page editor state or generating legacy Project JSON.
    pages.create_page(
        page_id=PAGE_ID, page_number=1, order_key="0001",
        page_purpose="Rendered persisted panels",
    )
    layouts.create_layout(
        layout_id="layout_1", page_id=PAGE_ID,
        geometry={"width": 300, "height": 200},
    )
    layouts.create_slot(
        slot_id="slot_red", layout_id="layout_1",
        slot_key="red", reading_order=1,
        geometry={"x": 20, "y": 20, "width": 70, "height": 70},
    )
    layouts.create_slot(
        slot_id="slot_blue", layout_id="layout_1",
        slot_key="blue", reading_order=2,
        geometry={"x": 205, "y": 105, "width": 65, "height": 65},
    )
    panels.create_panel(
        panel_id="panel_red", page_id=PAGE_ID, order_index=1,
        layout_slot_id="slot_red", panel_purpose="Red panel",
        action={"character_id": "hero", "action": "wave"},
        generation_spec={"seed": 42, "quality": "draft"},
    )
    panels.create_panel(
        panel_id="panel_blue", page_id=PAGE_ID, order_index=2,
        layout_slot_id="slot_blue", panel_purpose="Blue panel",
        action={"character_id": "rival", "action": "look"},
    )
    for ident, panel, color in (
        ("artifact_red", "panel_red", (220, 30, 45)),
        ("artifact_blue", "panel_blue", (25, 45, 225)),
    ):
        artifacts.register_local_bytes(
            artifact_id=ident,
            relative_path=f"assets/panels/{ident}.png",
            artifact_type="panel_candidate",
            scope_type="panel",
            scope_id=panel,
            mime_type="image/png",
            data=_png(color),
            dependency_fingerprint=f"fake-input:{ident}",
        )
        saved_panel = panels.get_panel(panel)
        panels.update_panel(
            panel, expected_revision=saved_panel["revision"],
            selected_candidate_id=ident,
        )

    # No legacy Project state for the test to accidentally consume.
    for name in ("project.json", "pages.json", "panels.json"):
        assert not (handle.root / name).exists()
    assert not (tmp_path / "projects" / WORK_ID).exists()

    preferences = tmp_path / "browser-local-storage.json"
    preferences.write_text("{}", encoding="utf-8")

    first_server = await aiohttp_server(_app(tmp_path))
    first_origin = str(first_server.make_url("/")).rstrip("/")
    first = await _run_browser(node, "first", first_origin, preferences)
    assert first["slot_x"] == 80
    assert first["page_width"] == 300
    assert first["registered_count"] == 1
    _assert_export_file(
        handle.root, artifacts, first,
        width=300, red_xy=(110, 55), white_xy=(55, 55),
    )
    assert len(artifacts.list_for_scope("page", PAGE_ID)) == 1

    # Artifacts belong to a Work and an individual Panel: other Work IDs
    # cannot retrieve page images, and a wrong-Panel selected candidate
    # cannot pass export, even though the candidate file exists.
    async with ClientSession() as session:
        wrong_work = await session.get(
            first_origin + "/manga_autopilot/api/v2/works/work_unrelated"
            + "/exports/" + first["artifact_id"] + "/png"
        )
        assert wrong_work.status == 404
        wrong_work.release()
        wrong_owner = panels.get_panel("panel_red")
        panels.update_panel(
            "panel_red", expected_revision=wrong_owner["revision"],
            selected_candidate_id="artifact_blue",
        )
        failed = await session.post(
            first_origin + f"/manga_autopilot/api/v2/works/{WORK_ID}"
            + f"/pages/{PAGE_ID}/export/png",
            json={},
        )
        assert failed.status == 422
        failure = await failed.json()
        assert "owned by this Panel" in failure["message"]
        assert len(artifacts.list_for_scope("page", PAGE_ID)) == 1

    with pytest.raises(ValueError, match="relative_path"):
        artifacts.register_local_bytes(
            artifact_id="forbidden_escape", data=_png((1, 2, 3)),
            relative_path="exports/../forbidden.png",
            artifact_type="page_render",
            scope_type="page", scope_id=PAGE_ID,
            mime_type="image/png",
            dependency_fingerprint="invalid-path",
        )
    assert not (handle.root / "forbidden.png").exists()

    # Restore valid selection by persisted repository revision, not a mock
    # browser attribute. Then really close the aiohttp server and open Work
    # in a newly constructed lifecycle/application instance.
    changed = panels.get_panel("panel_red")
    panels.update_panel(
        "panel_red", expected_revision=changed["revision"],
        selected_candidate_id="artifact_red",
    )
    await first_server.close()
    reopened = WorkLifecycleRepository(tmp_path).open_work(WORK_ID)
    assert reopened.database_path == handle.database_path
    assert reopened.work_id == WORK_ID

    second_server = await aiohttp_server(_app(tmp_path))
    second_origin = str(second_server.make_url("/")).rstrip("/")
    assert second_server is not first_server, "the HTTP application must restart"
    second = await _run_browser(node, "reopen", second_origin, preferences)
    assert second["slot_x"] == 152
    assert second["page_width"] == 360
    assert second["slot_revision"] == first["slot_revision"] + 1
    assert second["registered_count"] == 2
    assert second["artifact_id"] != first["artifact_id"]
    assert second["relative_path"] != first["relative_path"]
    assert second["sha256"] != first["sha256"]
    assert second["dependency_fingerprint"] != first["dependency_fingerprint"]
    _assert_export_file(
        handle.root, artifacts, second,
        width=360, red_xy=(180, 55), white_xy=(110, 55),
    )
    # No overwriting: first export remains byte-for-byte valid.
    _assert_export_file(
        handle.root, artifacts, first,
        width=300, red_xy=(110, 55), white_xy=(55, 55),
    )

    with repository_read(reopened.database_path) as db:
        layout = db.execute(
            "SELECT * FROM layout_instances WHERE id = 'layout_1'"
        ).fetchone()
        red_slot = db.execute(
            "SELECT * FROM layout_slots WHERE id = 'slot_red'"
        ).fetchone()
        red_panel = db.execute(
            "SELECT * FROM panels WHERE id = 'panel_red'"
        ).fetchone()
        page_exports = db.execute(
            """SELECT * FROM artifacts WHERE scope_type = 'page'
               AND scope_id = ? ORDER BY created_commit_seq""",
            (PAGE_ID,),
        ).fetchall()
    assert json.loads(layout["geometry_json"]) == {"width": 360, "height": 200}
    assert json.loads(red_slot["geometry_json"])["x"] == 152
    assert red_panel["selected_candidate_id"] == "artifact_red"
    assert json.loads(red_panel["action_json"]) == {
        "character_id": "hero", "action": "wave",
    }
    assert json.loads(red_panel["generation_spec_json"]) == {
        "seed": 42, "quality": "draft",
    }
    assert {row["id"] for row in page_exports} == {
        first["artifact_id"], second["artifact_id"],
    }
    assert all(row["revision"] == 1 for row in page_exports)
    assert all(row["created_commit_seq"] for row in page_exports)
    assert all(row["dependency_fingerprint"] for row in page_exports)
    for name in ("project.json", "pages.json", "panels.json"):
        assert not (reopened.root / name).exists()
    assert not (tmp_path / "projects" / WORK_ID).exists()
