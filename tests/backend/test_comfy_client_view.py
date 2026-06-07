"""Tests for ComfyClient /view image fetch + save."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from manga_autopilot.services.comfy_client import ComfyClient


@pytest.fixture()
async def view_server(aiohttp_server):
    seen: dict[str, dict[str, str]] = {}

    async def handle_view(request: web.Request) -> web.Response:
        params = dict(request.query)
        seen["last"] = params
        return web.Response(body=b"PNG-BYTES", content_type="image/png")

    app = web.Application()
    app["seen"] = seen
    app.router.add_get("/view", handle_view)
    return await aiohttp_server(app)


async def _make_client(server) -> ComfyClient:
    base = f"http://{server.host}:{server.port}"
    return ComfyClient(base_url=base, timeout_sec=5)


async def test_fetch_view_returns_bytes(view_server) -> None:
    async with await _make_client(view_server) as client:
        data = await client.fetch_view("img.png", subfolder="sub", type="output")
        assert data == b"PNG-BYTES"
        params = view_server.app["seen"]["last"]
        assert params == {"filename": "img.png", "type": "output", "subfolder": "sub"}


async def test_fetch_view_skips_empty_subfolder(view_server) -> None:
    async with await _make_client(view_server) as client:
        await client.fetch_view("img.png")
        params = view_server.app["seen"]["last"]
        assert params == {"filename": "img.png", "type": "output"}


async def test_fetch_view_requires_filename() -> None:
    client = ComfyClient(base_url="http://example.com")
    with pytest.raises(ValueError):
        await client.fetch_view("")


async def test_fetch_image_to_saves_locally(view_server, tmp_path: Path) -> None:
    async with await _make_client(view_server) as client:
        dest = tmp_path / "nested" / "img.png"
        result = await client.fetch_image_to(dest, filename="img.png")
        assert result == dest
        assert dest.read_bytes() == b"PNG-BYTES"
