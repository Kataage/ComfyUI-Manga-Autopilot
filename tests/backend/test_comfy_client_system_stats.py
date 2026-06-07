"""Tests for ComfyClient /system_stats, /devices, /extensions."""

from __future__ import annotations

import pytest
from aiohttp import web

from manga_autopilot.services.comfy_client import ComfyClient


@pytest.fixture()
async def stats_server(aiohttp_server):
    async def handle_system_stats(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "system": {"os": "linux", "python_version": "3.12.0"},
                "devices": [
                    {"name": "NVIDIA GeForce RTX 4090", "type": "cuda", "vram_total": 25769803776}
                ],
            }
        )

    async def handle_devices(_: web.Request) -> web.Response:
        return web.json_response(
            [
                {"name": "NVIDIA GeForce RTX 4090", "type": "cuda", "index": 0},
                {"name": "CPU", "type": "cpu", "index": 1},
            ]
        )

    async def handle_extensions(_: web.Request) -> web.Response:
        return web.json_response(["manga_autopilot.api.health"])

    app = web.Application()
    app.router.add_get("/system_stats", handle_system_stats)
    app.router.add_get("/devices", handle_devices)
    app.router.add_get("/extensions", handle_extensions)
    return await aiohttp_server(app)


async def _make_client(server) -> ComfyClient:
    base = f"http://{server.host}:{server.port}"
    return ComfyClient(base_url=base, timeout_sec=5)


async def test_get_system_stats(stats_server) -> None:
    async with await _make_client(stats_server) as client:
        stats = await client.get_system_stats()
        assert stats["system"]["os"] == "linux"
        assert stats["devices"][0]["type"] == "cuda"


async def test_get_devices(stats_server) -> None:
    async with await _make_client(stats_server) as client:
        devices = await client.get_devices()
        assert [d["name"] for d in devices] == ["NVIDIA GeForce RTX 4090", "CPU"]


async def test_get_extensions(stats_server) -> None:
    async with await _make_client(stats_server) as client:
        ext = await client.get_extensions()
        assert ext == ["manga_autopilot.api.health"]


async def test_get_server_info(stats_server) -> None:
    async with await _make_client(stats_server) as client:
        info = await client.get_server_info()
        assert info["system_stats"]["system"]["os"] == "linux"
        assert len(info["devices"]) == 2
