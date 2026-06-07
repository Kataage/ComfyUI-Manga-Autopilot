"""Tests for the workflow executor / dispatcher."""

from __future__ import annotations

import pytest
from aiohttp import web

from manga_autopilot.models.workflow import WorkflowDefinition, WorkflowType
from manga_autopilot.services.comfy_client import ComfyClient
from manga_autopilot.services.workflow_executor import (
    apply_overrides,
    dispatch_test_run,
)


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="wf",
        name="wf",
        type=WorkflowType.TEXT_TO_IMAGE,
        file="wf.json",
        bindings={
            "positive_prompt": {"node_id": "6", "input": "text"},
            "negative_prompt": {"node_id": "7", "input": "text"},
            "seed": {"node_id": "3", "input": "seed"},
            "width": {"node_id": "5", "input": "width"},
            "height": {"node_id": "5", "input": "height"},
            "filename_prefix": {"node_id": "9", "input": "filename_prefix"},
        },
    )


def _graph() -> dict:
    return {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512},
        },
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "b"}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}},
    }


@pytest.fixture()
async def prompt_server(aiohttp_server):
    captured: dict = {}

    async def handle_prompt(request: web.Request) -> web.Response:
        body = await request.json()
        captured["prompt"] = body
        return web.json_response({"prompt_id": "p-1"})

    async def handle_history(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "p-1": {
                    "outputs": {
                        "9": {
                            "images": [
                                {"filename": "out.png", "subfolder": "", "type": "output"}
                            ]
                        }
                    }
                }
            }
        )

    async def handle_object_info(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "KSampler": {"input": {"required": {"seed": ("INT",)}}},
                "EmptyLatentImage": {
                    "input": {"required": {"width": ("INT",), "height": ("INT",)}}
                },
                "CLIPTextEncode": {"input": {"required": {"text": ("STRING",)}}},
                "SaveImage": {
                    "input": {"required": {"filename_prefix": ("STRING",)}}
                },
            }
        )

    app = web.Application()
    app["captured"] = captured
    app.router.add_post("/prompt", handle_prompt)
    app.router.add_get("/history/{prompt_id}", handle_history)
    app.router.add_get("/history", handle_history)
    app.router.add_get("/object_info", handle_object_info)
    return await aiohttp_server(app)


async def _client(server) -> ComfyClient:
    base = f"http://{server.host}:{server.port}"
    return ComfyClient(base_url=base, timeout_sec=5)


def test_apply_overrides_replaces_inputs() -> None:
    wf = _workflow()
    out = apply_overrides(
        _graph(),
        wf.bindings,
        {"positive_prompt": "a hero", "seed": 7, "width": 640, "height": 960},
    )
    assert out["6"]["inputs"]["text"] == "a hero"
    assert out["3"]["inputs"]["seed"] == 7
    assert out["5"]["inputs"]["width"] == 640
    assert out["5"]["inputs"]["height"] == 960


def test_apply_overrides_without_overrides_copies_graph() -> None:
    wf = _workflow()
    out = apply_overrides(_graph(), wf.bindings, None)
    assert out == _graph()
    # Copies are independent.
    out["6"]["inputs"]["text"] = "mutated"
    assert _graph()["6"]["inputs"]["text"] == "a"


async def test_dispatch_test_run_success(prompt_server) -> None:
    async with await _client(prompt_server) as client:
        info = await client.get_object_info()
        result = await dispatch_test_run(
            _workflow(),
            _graph(),
            client=client,
            overrides={"positive_prompt": "hero"},
            object_info=info,
        )
    assert result.ok is True
    assert result.prompt_id == "p-1"
    assert result.image_refs and result.image_refs[0]["filename"] == "out.png"


async def test_dispatch_test_run_validates_object_info(prompt_server) -> None:
    async with await _client(prompt_server) as client:
        bad_graph = _graph()
        bad_graph["3"]["class_type"] = "Unknown"
        result = await dispatch_test_run(
            _workflow(), bad_graph, client=client, object_info={}
        )
    assert result.ok is False
    assert any("Unknown" in e for e in result.errors)


async def test_dispatch_test_run_rejects_missing_api_graph(prompt_server) -> None:
    async with await _client(prompt_server) as client:
        result = await dispatch_test_run(_workflow(), None, client=client)
    assert result.ok is False


async def test_dispatch_test_run_rejects_bad_graph(prompt_server) -> None:
    async with await _client(prompt_server) as client:
        bad = {"3": "not a dict"}
        result = await dispatch_test_run(_workflow(), bad, client=client)
    assert result.ok is False
    assert any("invalid api_graph" in e for e in result.errors)
