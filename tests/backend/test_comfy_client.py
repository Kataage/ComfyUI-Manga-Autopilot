"""Tests for the ComfyClient HTTP transport."""

from __future__ import annotations

import pytest
from aiohttp import web

from manga_autopilot.services.comfy_client import ComfyClient, ComfyUIRequestError


@pytest.fixture()
async def comfy_server(aiohttp_server):
    async def handle_json(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "method": "get"})

    async def handle_echo(request: web.Request) -> web.Response:
        data = await request.json()
        return web.json_response({"echo": data})

    async def handle_bytes(_request: web.Request) -> web.Response:
        return web.Response(body=b"binarycontent", content_type="image/png")

    async def handle_error(_request: web.Request) -> web.Response:
        return web.json_response({"error": "broken"}, status=500)

    async def handle_text(_request: web.Request) -> web.Response:
        return web.Response(text="not json", content_type="text/plain")

    async def handle_multipart(request: web.Request) -> web.Response:
        reader = await request.multipart()
        seen: list[str] = []
        async for part in reader:
            seen.append(part.name or "")
            await part.read()
        return web.json_response({"fields": seen})

    app = web.Application()
    app.router.add_get("/ok", handle_json)
    app.router.add_post("/echo", handle_echo)
    app.router.add_get("/img", handle_bytes)
    app.router.add_get("/boom", handle_error)
    app.router.add_get("/text", handle_text)
    app.router.add_post("/upload", handle_multipart)
    return await aiohttp_server(app)


async def _make_client(server) -> ComfyClient:
    base = f"http://{server.host}:{server.port}"
    return ComfyClient(base_url=base, timeout_sec=5)


async def test_get_json_returns_payload(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        body = await client.get_json("/ok")
        assert body == {"ok": True, "method": "get"}


async def test_post_json_round_trip(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        body = await client.post_json("/echo", {"foo": 1})
        assert body == {"echo": {"foo": 1}}


async def test_get_bytes_returns_raw(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        body = await client.get_bytes("/img")
        assert body == b"binarycontent"


async def test_error_status_raises(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        with pytest.raises(ComfyUIRequestError) as exc:
            await client.get_json("/boom")
        assert exc.value.status == 500
        assert "broken" in (exc.value.body or "")


async def test_non_json_response_raises(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        with pytest.raises(ComfyUIRequestError):
            await client.get_json("/text")


async def test_multipart_post(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        body = await client.post_multipart(
            "/upload",
            {"image": ("ref.png", b"\x00\x01", "image/png"), "type": "input"},
        )
        assert set(body["fields"]) == {"image", "type"}


def test_url_join_handles_missing_slash() -> None:
    client = ComfyClient(base_url="http://example.com")
    assert client.url("history") == "http://example.com/history"
    assert client.url("/history") == "http://example.com/history"
