"""Tests for ComfyClient /prompt submission."""

from __future__ import annotations

import pytest
from aiohttp import web

from manga_autopilot.services.comfy_client import ComfyClient, ComfyUIRequestError


@pytest.fixture()
async def comfy_server(aiohttp_server):
    state: dict[str, object] = {"last_payload": None, "mode": "ok"}

    async def handle_prompt(request: web.Request) -> web.Response:
        payload = await request.json()
        state["last_payload"] = payload
        mode = state.get("mode")
        if mode == "validation_error":
            return web.json_response(
                {"error": "PROMPT_VALIDATION_ERROR", "node_errors": {}},
                status=400,
            )
        if mode == "no_prompt_id":
            return web.json_response({"number": 1})
        return web.json_response(
            {"prompt_id": "prompt-123", "number": 7, "node_errors": {}}
        )

    async def handle_queue(_request: web.Request) -> web.Response:
        return web.json_response(
            {"queue_running": [["prompt-123", 1]], "queue_pending": []}
        )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/prompt", handle_prompt)
    app.router.add_get("/queue", handle_queue)
    return await aiohttp_server(app)


async def _make_client(server) -> ComfyClient:
    base = f"http://{server.host}:{server.port}"
    return ComfyClient(base_url=base, timeout_sec=5, client_id="test_client")


WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 1,
            "steps": 4,
            "cfg": 7,
            "sampler_name": "dpmpp_2m",
            "scheduler": "karras",
        },
    }
}


async def test_submit_workflow_returns_prompt_id(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        prompt_id = await client.submit_workflow(WORKFLOW)
        assert prompt_id == "prompt-123"
        payload = comfy_server.app["state"]["last_payload"]
        assert payload["client_id"] == "test_client"
        assert payload["prompt"] == WORKFLOW


async def test_submit_workflow_passes_extra_data(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        await client.submit_workflow(WORKFLOW, extra_data={"foo": 1})
        payload = comfy_server.app["state"]["last_payload"]
        assert payload["extra_data"] == {"foo": 1}


async def test_submit_workflow_rejects_empty() -> None:
    client = ComfyClient(base_url="http://example.com")
    with pytest.raises(ValueError):
        await client.submit_workflow({})


async def test_submit_workflow_raises_on_validation_error(comfy_server) -> None:
    comfy_server.app["state"]["mode"] = "validation_error"
    async with await _make_client(comfy_server) as client:
        with pytest.raises(ComfyUIRequestError):
            await client.submit_workflow(WORKFLOW)


async def test_submit_workflow_raises_without_prompt_id(comfy_server) -> None:
    comfy_server.app["state"]["mode"] = "no_prompt_id"
    async with await _make_client(comfy_server) as client:
        with pytest.raises(ComfyUIRequestError):
            await client.submit_workflow(WORKFLOW)


async def test_get_queue_state(comfy_server) -> None:
    async with await _make_client(comfy_server) as client:
        state = await client.get_queue_state()
        assert "queue_running" in state
