"""Tests for the health route."""

from __future__ import annotations

import pytest
from aiohttp import web

from manga_autopilot import __version__
from manga_autopilot.routes import health_routes


@pytest.fixture()
async def client(aiohttp_client):
    app = web.Application()
    health_routes.register(app.router)
    return await aiohttp_client(app)


async def test_health_returns_ok(client) -> None:
    resp = await client.get(health_routes.HEALTH_PATH)
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["service"] == "manga_autopilot"
    assert body["version"] == __version__
    assert "uptime_sec" in body
    assert isinstance(body["uptime_sec"], (int, float))


async def test_health_path_is_namespaced() -> None:
    assert health_routes.HEALTH_PATH.startswith("/manga_autopilot/api/")
