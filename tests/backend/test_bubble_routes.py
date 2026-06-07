"""Tests for the bubble HTTP API."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from manga_autopilot.routes import register_all


@pytest.fixture()
async def client(aiohttp_client, tmp_path: Path):
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.json").write_text("{}", encoding="utf-8")
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    return await aiohttp_client(app)


def _payload(bid: str = "b1", pid: str = "p1", text: str = "hi") -> dict:
    return {
        "id": bid,
        "panel_id": pid,
        "text": text,
        "type": "normal",
        "direction": "vertical",
        "font": {"family": "NotoSansJP", "size": 18, "color": "#000000"},
    }


async def test_create_list_get_update_delete(client) -> None:
    # Create
    resp = await client.post(
        "/manga_autopilot/api/projects/demo/bubbles", json=_payload()
    )
    assert resp.status == 201

    # List
    resp = await client.get("/manga_autopilot/api/projects/demo/bubbles")
    body = await resp.json()
    assert len(body) == 1 and body[0]["id"] == "b1"

    # Filter by panel
    await client.post(
        "/manga_autopilot/api/projects/demo/bubbles", json=_payload("b2", "p2")
    )
    resp = await client.get(
        "/manga_autopilot/api/projects/demo/bubbles?panel_id=p1"
    )
    body = await resp.json()
    assert {b["id"] for b in body} == {"b1"}

    # Update
    resp = await client.put(
        "/manga_autopilot/api/projects/demo/bubbles/b1",
        json=_payload(text="updated"),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["text"] == "updated"

    # Delete
    resp = await client.delete(
        "/manga_autopilot/api/projects/demo/bubbles/b1"
    )
    assert resp.status == 204

    # Delete missing
    resp = await client.delete(
        "/manga_autopilot/api/projects/demo/bubbles/missing"
    )
    assert resp.status == 404


async def test_create_invalid_payload(client) -> None:
    resp = await client.post(
        "/manga_autopilot/api/projects/demo/bubbles", json={"id": "x"}
    )
    assert resp.status == 400


async def test_create_bad_json(client) -> None:
    resp = await client.post(
        "/manga_autopilot/api/projects/demo/bubbles",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
