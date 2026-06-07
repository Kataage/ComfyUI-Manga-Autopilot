"""Tests for ComfyClient /object_info."""

from __future__ import annotations

import pytest
from aiohttp import web

from manga_autopilot.services.comfy_client import ComfyClient


@pytest.fixture()
async def object_info_server(aiohttp_server):
    registry: dict[str, dict] = {
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": (["model.safetensors"],)}},
            "output": ["MODEL", "CLIP", "VAE"],
            "output_name": ["MODEL", "CLIP", "VAE"],
            "name": "CheckpointLoaderSimple",
            "display_name": "Load Checkpoint",
            "description": "",
            "category": "loaders",
        },
        "KSampler": {
            "input": {"required": {"seed": ("INT",)}},
            "output": ["LATENT"],
            "output_name": ["LATENT"],
            "name": "KSampler",
            "display_name": "KSampler",
            "description": "",
            "category": "sampling",
        },
    }

    async def handle_object_info(request: web.Request) -> web.Response:
        node_class = request.query.get("node_class")
        if node_class:
            entry = registry.get(node_class)
            return web.json_response({node_class: entry} if entry else {})
        return web.json_response(registry)

    app = web.Application()
    app.router.add_get("/object_info", handle_object_info)
    return await aiohttp_server(app)


async def _make_client(server) -> ComfyClient:
    base = f"http://{server.host}:{server.port}"
    return ComfyClient(base_url=base, timeout_sec=5)


async def test_get_object_info_full(object_info_server) -> None:
    async with await _make_client(object_info_server) as client:
        info = await client.get_object_info()
        assert set(info) == {"CheckpointLoaderSimple", "KSampler"}


async def test_get_object_info_filtered(object_info_server) -> None:
    async with await _make_client(object_info_server) as client:
        entry = await client.get_object_info("KSampler")
        assert entry["display_name"] == "KSampler"
        assert entry["category"] == "sampling"


async def test_get_object_info_missing_returns_empty(object_info_server) -> None:
    async with await _make_client(object_info_server) as client:
        entry = await client.get_object_info("UnknownNode")
        assert entry == {}


async def test_has_node(object_info_server) -> None:
    async with await _make_client(object_info_server) as client:
        assert await client.has_node("KSampler") is True
        assert await client.has_node("NoSuchNode") is False


async def test_list_node_classes(object_info_server) -> None:
    async with await _make_client(object_info_server) as client:
        classes = await client.list_node_classes()
        assert set(classes) == {"CheckpointLoaderSimple", "KSampler"}
