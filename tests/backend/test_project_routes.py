"""Tests for the project CRUD HTTP API (spec section 21.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from manga_autopilot.routes import register_all


@pytest.fixture()
async def client(aiohttp_client, tmp_path: Path):
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    cli = await aiohttp_client(app)
    return cli, tmp_path


# --------------------------------------------------------- list
async def test_list_projects_empty(client) -> None:
    cli, _tmp = client
    resp = await cli.get("/manga_autopilot/api/projects")
    assert resp.status == 200
    assert await resp.json() == []


# --------------------------------------------------------- create
async def test_create_project_persists_files(client) -> None:
    cli, tmp_path = client
    resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "My Manga", "idea": "hero's journey", "title": "First"},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["id"].startswith("proj_")
    assert body["name"] == "My Manga"
    assert body["title"] == "First"
    # The project.json was written.
    project_dir = tmp_path / "projects" / body["id"]
    assert (project_dir / "project.json").exists()


async def test_create_project_rejects_missing_name(client) -> None:
    cli, _tmp = client
    resp = await cli.post("/manga_autopilot/api/projects", json={})
    assert resp.status == 400


async def test_create_project_uses_supplied_id(client) -> None:
    cli, tmp_path = client
    pid = "proj_test_001"
    resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "Test", "id": pid},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["id"] == pid
    assert (tmp_path / "projects" / pid / "project.json").exists()


async def test_create_project_duplicate_returns_409(client) -> None:
    cli, _tmp = client
    pid = "proj_test_001"
    resp1 = await cli.post(
        "/manga_autopilot/api/projects", json={"name": "A", "id": pid}
    )
    assert resp1.status == 201
    resp2 = await cli.post(
        "/manga_autopilot/api/projects", json={"name": "B", "id": pid}
    )
    assert resp2.status == 409


async def test_create_project_rejects_unsafe_id(client) -> None:
    cli, _tmp = client
    resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "X", "id": "../escape"},
    )
    assert resp.status == 400


# --------------------------------------------------------- get
async def test_get_project(client) -> None:
    cli, _tmp = client
    create = await cli.post(
        "/manga_autopilot/api/projects", json={"name": "A"}
    )
    pid = (await create.json())["id"]
    resp = await cli.get(f"/manga_autopilot/api/projects/{pid}")
    assert resp.status == 200
    body = await resp.json()
    assert body["id"] == pid
    assert body["name"] == "A"


async def test_get_project_missing_returns_404(client) -> None:
    cli, _tmp = client
    resp = await cli.get("/manga_autopilot/api/projects/proj_nope")
    assert resp.status == 404


# --------------------------------------------------------- patch
async def test_patch_project_updates_fields(client) -> None:
    cli, _tmp = client
    create = await cli.post(
        "/manga_autopilot/api/projects", json={"name": "Original"}
    )
    pid = (await create.json())["id"]
    resp = await cli.patch(
        f"/manga_autopilot/api/projects/{pid}",
        json={"title": "Renamed", "status": "STORY_PLANNED"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["title"] == "Renamed"
    assert body["status"] == "STORY_PLANNED"


async def test_patch_project_missing_returns_404(client) -> None:
    cli, _tmp = client
    resp = await cli.patch(
        "/manga_autopilot/api/projects/proj_nope", json={"title": "X"}
    )
    assert resp.status == 404


# --------------------------------------------------------- delete
async def test_delete_project_removes_dir(client) -> None:
    cli, tmp_path = client
    create = await cli.post(
        "/manga_autopilot/api/projects", json={"name": "A"}
    )
    pid = (await create.json())["id"]
    resp = await cli.delete(f"/manga_autopilot/api/projects/{pid}")
    assert resp.status == 200
    assert not (tmp_path / "projects" / pid).exists()


async def test_delete_project_missing_returns_404(client) -> None:
    cli, _tmp = client
    resp = await cli.delete("/manga_autopilot/api/projects/proj_nope")
    assert resp.status == 404


# --------------------------------------------------------- suggest
async def test_suggest_project_id(client) -> None:
    cli, _tmp = client
    resp = await cli.get("/manga_autopilot/api/projects/_suggest_id")
    assert resp.status == 200
    body = await resp.json()
    # Each call must return a fresh, well-formed id.
    assert body["id"].startswith("proj_")
    resp2 = await cli.get("/manga_autopilot/api/projects/_suggest_id")
    body2 = await resp2.json()
    assert body2["id"] != body["id"]
