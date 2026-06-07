"""Tests for the workflow HTTP API (spec section 21.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from manga_autopilot.routes import register_all


@pytest.fixture()
async def client(aiohttp_client, tmp_path: Path):
    app = web.Application()
    register_all(app, storage_root=str(tmp_path))
    client = await aiohttp_client(app)
    return client


def _payload(wid: str = "anime_t2i_default") -> dict:
    return {
        "workflow_id": wid,
        "name": f"Workflow {wid}",
        "type": "text_to_image",
        "file": f"workflows/{wid}_api.json",
        "bindings": {
            "positive_prompt": {"node_id": "6", "input": "text"},
            "negative_prompt": {"node_id": "7", "input": "text"},
            "seed": {"node_id": "3", "input": "seed"},
            "width": {"node_id": "5", "input": "width"},
            "height": {"node_id": "5", "input": "height"},
            "filename_prefix": {"node_id": "9", "input": "filename_prefix"},
        },
    }


async def test_list_workflows_empty(client) -> None:
    resp = await client.get("/manga_autopilot/api/workflows")
    assert resp.status == 200
    assert await resp.json() == []


async def test_create_workflow(client) -> None:
    resp = await client.post("/manga_autopilot/api/workflows", json=_payload())
    assert resp.status == 201
    body = await resp.json()
    assert body["workflow_id"] == "anime_t2i_default"


async def test_create_workflow_conflict(client) -> None:
    await client.post("/manga_autopilot/api/workflows", json=_payload())
    resp = await client.post("/manga_autopilot/api/workflows", json=_payload())
    assert resp.status == 409


async def test_create_workflow_invalid(client) -> None:
    resp = await client.post("/manga_autopilot/api/workflows", json={"workflow_id": "x"})
    assert resp.status == 400


async def test_create_workflow_bad_json(client) -> None:
    resp = await client.post(
        "/manga_autopilot/api/workflows",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_get_workflow(client) -> None:
    await client.post("/manga_autopilot/api/workflows", json=_payload())
    resp = await client.get("/manga_autopilot/api/workflows/anime_t2i_default")
    assert resp.status == 200
    body = await resp.json()
    assert body["name"] == "Workflow anime_t2i_default"


async def test_get_workflow_missing(client) -> None:
    resp = await client.get("/manga_autopilot/api/workflows/missing")
    assert resp.status == 404


async def test_update_workflow(client) -> None:
    await client.post("/manga_autopilot/api/workflows", json=_payload())
    body = _payload()
    body["name"] = "Renamed"
    resp = await client.patch(
        "/manga_autopilot/api/workflows/anime_t2i_default", json=body
    )
    assert resp.status == 200
    updated = await resp.json()
    assert updated["name"] == "Renamed"


async def test_update_workflow_missing(client) -> None:
    resp = await client.patch(
        "/manga_autopilot/api/workflows/missing", json=_payload("missing")
    )
    assert resp.status == 404


async def test_delete_workflow(client) -> None:
    await client.post("/manga_autopilot/api/workflows", json=_payload())
    resp = await client.delete("/manga_autopilot/api/workflows/anime_t2i_default")
    assert resp.status == 204
    resp = await client.get("/manga_autopilot/api/workflows/anime_t2i_default")
    assert resp.status == 404


async def test_validate_workflow(client) -> None:
    payload = _payload()
    payload["api_graph"] = {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
    }
    await client.post("/manga_autopilot/api/workflows", json=payload)
    resp = await client.post(
        "/manga_autopilot/api/workflows/anime_t2i_default/validate"
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert set(body["nodes"]) == {"3", "9"}


async def test_validate_workflow_bad_graph(client, tmp_path) -> None:
    payload = _payload()
    payload["api_graph"] = {"3": "not an object"}
    # We need a path-aware registry; use the same client, then write to disk
    # directly to force the validator to find a bad graph.
    await client.post("/manga_autopilot/api/workflows", json=payload)
    import json

    wf_path = tmp_path / "workflows" / "anime_t2i_default.json"
    raw = json.loads(wf_path.read_text("utf-8"))
    raw["api_graph"] = {"3": "not an object"}
    wf_path.write_text(json.dumps(raw), encoding="utf-8")
    resp = await client.post(
        "/manga_autopilot/api/workflows/anime_t2i_default/validate"
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is False


async def test_test_run_workflow_missing_graph(client) -> None:
    await client.post("/manga_autopilot/api/workflows", json=_payload())
    resp = await client.post(
        "/manga_autopilot/api/workflows/anime_t2i_default/test-run", json={}
    )
    assert resp.status == 400


async def test_test_run_workflow_submits_and_saves(client, monkeypatch, tmp_path) -> None:
    payload = _payload()
    payload["api_graph"] = {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "1girl"}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "manga"}},
    }
    await client.post("/manga_autopilot/api/workflows", json=payload)

    class _FakeComfyClient:
        def __init__(self) -> None:
            self.submitted: list[dict] = []
            self.view_calls: list[dict] = []

        async def submit_workflow(self, graph, **kwargs):
            self.submitted.append(graph)
            return "prompt-abc"

        async def get_history(self, prompt_id):
            assert prompt_id == "prompt-abc"
            return {
                "prompt-abc": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {
                        "9": {
                            "images": [
                                {"filename": "page_0001.png", "subfolder": "", "type": "output"}
                            ]
                        }
                    },
                }
            }

        async def fetch_image_to(self, dest, *, filename, subfolder, type):
            self.view_calls.append(
                {"dest": str(dest), "filename": filename, "subfolder": subfolder, "type": type}
            )
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
            return Path(dest)

    fake = _FakeComfyClient()
    client.app["manga_comfy_client"] = fake

    resp = await client.post(
        "/manga_autopilot/api/workflows/anime_t2i_default/test-run",
        json={"overrides": {"positive_prompt": "1girl, blue hair"}, "output_dir": str(tmp_path)},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["prompt_id"] == "prompt-abc"
    assert body["images_saved"]
    assert fake.submitted and fake.submitted[0]["6"]["inputs"]["text"] == "1girl, blue hair"
    saved = Path(body["images_saved"][0])
    assert saved.exists()
    assert saved.parent == tmp_path


async def test_test_run_workflow_propagates_error(client) -> None:
    payload = _payload()
    payload["api_graph"] = {
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}},
    }
    await client.post("/manga_autopilot/api/workflows", json=payload)

    from manga_autopilot.services.comfy_client import ComfyUIRequestError

    class _FakeClient:
        async def submit_workflow(self, graph, **kwargs):
            raise ComfyUIRequestError("rejected", status=400, body="bad prompt")

    client.app["manga_comfy_client"] = _FakeClient()
    resp = await client.post(
        "/manga_autopilot/api/workflows/anime_t2i_default/test-run", json={}
    )
    assert resp.status == 502
    body = await resp.json()
    assert body["ok"] is False
    assert "rejected" in body["error"]
