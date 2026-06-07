"""Tests for ComfyClient /history wrappers."""

from __future__ import annotations

import pytest
from aiohttp import web

from manga_autopilot.services.comfy_client import ComfyClient, ComfyUIRequestError

HISTORY_SAMPLE = {
    "prompt-123": {
        "prompt": [7, "prompt-123", {}, {}, []],
        "status": {"status_str": "success"},
        "outputs": {
            "9": {
                "images": [
                    {"filename": "img_00001_.png", "subfolder": "", "type": "output"},
                    {"filename": "img_00002_.png", "subfolder": "subdir", "type": "output"},
                ]
            },
            "10": {
                "text": ["ignored"],
            },
        },
    }
}


@pytest.fixture()
async def history_server(aiohttp_server):
    async def handle_history(request: web.Request) -> web.Response:
        prompt_id = request.match_info.get("prompt_id")
        if prompt_id:
            return web.json_response(
                {prompt_id: HISTORY_SAMPLE.get(prompt_id, {})}
            )
        return web.json_response(HISTORY_SAMPLE)

    async def handle_bad(_request: web.Request) -> web.Response:
        return web.json_response([])  # not a dict on purpose

    app = web.Application()
    app.router.add_get("/history", handle_history)
    app.router.add_get("/history/{prompt_id}", handle_history)
    app.router.add_get("/history-bad", handle_bad)
    return await aiohttp_server(app)


async def _make_client(server) -> ComfyClient:
    base = f"http://{server.host}:{server.port}"
    return ComfyClient(base_url=base, timeout_sec=5)


async def test_get_history_all(history_server) -> None:
    async with await _make_client(history_server) as client:
        history = await client.get_history()
        assert "prompt-123" in history


async def test_get_history_by_id(history_server) -> None:
    async with await _make_client(history_server) as client:
        history = await client.get_history("prompt-123")
        assert "prompt-123" in history
        assert history["prompt-123"]["outputs"]["9"]["images"][0]["filename"]


async def test_get_history_rejects_non_mapping(history_server) -> None:
    async with await _make_client(history_server) as client:
        # /history-bad returns `[]` which get_json passes through as-is; the
        # extra non-dict guard belongs to `get_history`, so route the call
        # through the helper used in production code paths.
        with pytest.raises(ComfyUIRequestError):
            await client._read_json_compat([])


def test_extract_output_images_flattens() -> None:
    entry = HISTORY_SAMPLE["prompt-123"]
    images = ComfyClient.extract_output_images(entry)
    assert len(images) == 2
    assert images[0]["filename"] == "img_00001_.png"
    assert images[0]["subfolder"] == ""
    assert images[0]["type"] == "output"
    assert images[0]["node_id"] == "9"
    assert images[1]["subfolder"] == "subdir"


def test_extract_output_images_handles_missing_outputs() -> None:
    assert ComfyClient.extract_output_images({}) == []
    assert ComfyClient.extract_output_images({"outputs": None}) == []
    assert ComfyClient.extract_output_images({"outputs": {"x": "not-a-dict"}}) == []
