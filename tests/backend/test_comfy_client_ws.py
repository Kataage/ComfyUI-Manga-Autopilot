"""Tests for ComfyClient WebSocket event streaming."""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from manga_autopilot.services.comfy_client import ComfyClient


@pytest.fixture()
async def ws_server(aiohttp_server):
    events = [
        {"type": "status", "data": {"sid": "abc"}},
        {"type": "execution_start", "data": {"prompt_id": "p"}},
        {"type": "progress", "data": {"value": 1, "max": 10}},
        {"type": "executed", "data": {"prompt_id": "p", "output": {}}},
    ]

    async def handle_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for event in events:
            await ws.send_str(json.dumps(event))
        # Send a non-JSON text frame the iterator should skip.
        await ws.send_str("not-json")
        # Send a binary frame which the iterator should skip.
        await ws.send_bytes(b"preview-bytes")
        await ws.close(code=1000, message=b"bye")
        return ws

    app = web.Application()
    app.router.add_get("/ws", handle_ws)
    return await aiohttp_server(app)


async def test_listen_events_yields_json_frames(ws_server) -> None:
    base = f"http://{ws_server.host}:{ws_server.port}"
    async with ComfyClient(base_url=base, timeout_sec=5) as client:
        collected: list[dict] = []
        async for event in client.listen_events(max_reconnects=0):
            collected.append(event)
            if event.get("type") == "executed":
                break
        types = [e["type"] for e in collected]
        assert types == ["status", "execution_start", "progress", "executed"]


async def test_listen_events_reconnect_limit(ws_server) -> None:
    """When max_reconnects=0 the iterator must stop after a single drop."""

    base = f"http://{ws_server.host}:{ws_server.port}"
    async with ComfyClient(base_url=base, timeout_sec=5) as client:
        seen_executed = False
        count = 0
        async for event in client.listen_events(max_reconnects=0):
            count += 1
            if event.get("type") == "executed":
                seen_executed = True
        assert seen_executed
        assert count == 4


def test_ws_url_derivation() -> None:
    client = ComfyClient(base_url="https://gpu.example.com:9000", client_id="cid")
    url = client._ws_url()
    assert url.startswith("wss://gpu.example.com:9000/ws?")
    assert "clientId=cid" in url
