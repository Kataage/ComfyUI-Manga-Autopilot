"""E2E tests exercising the ComfyExecutor + WorkflowRegistry + FakeComfyClient path.

These tests verify that the autopilot pipeline can drive image generation
through the real :class:`ComfyExecutor` → :class:`WorkflowRegistry` →
:class:`ComfyClient` submission path *without* a live ComfyUI server.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.routes import register_all
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings


# --------------------------------------------------------- fakes
def _parse_page_count(prompt: str) -> int:
    m = re.search(r"ページ数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


def _parse_panel_count(prompt: str) -> int:
    m = re.search(r"パネル数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


class FakeLLMProvider(LLMProvider):
    """LLM that returns canned plans matching the full autopilot pipeline."""

    def __init__(self, settings: LLMSettings | None = None) -> None:
        super().__init__(settings or LLMSettings())
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
    ) -> str:
        self.calls.append({"prompt": prompt, "schema_keys": list((schema or {}).keys())})
        required = (schema or {}).get("required", [])

        # Story planner
        if "title" in required and "pages" in required:
            pc = _parse_page_count(prompt)
            return json.dumps({
                "title": f"Sample {pc}-Page",
                "logline": "A hero adventure.",
                "genre": "fantasy",
                "pages": [
                    {
                        "pageNumber": i + 1,
                        "summary": f"Page {i + 1} summary",
                        "emotionalGoal": "determined",
                        "visualGoal": "wide shot",
                        "panelCount": 1,
                    }
                    for i in range(pc)
                ],
            })
        # Character planner
        if "characters" in required:
            return json.dumps({
                "characters": [
                    {
                        "id": "char_hero",
                        "name": "Hero",
                        "role": "protagonist",
                        "visualTraits": ["blue hair", "red scarf"],
                        "mustKeep": ["blue hair"],
                        "styleHints": "manga",
                    }
                ]
            })
        # Page planner
        if "pages" in required:
            pc = _parse_page_count(prompt)
            return json.dumps({
                "pages": [
                    {
                        "pageNumber": i + 1,
                        "summary": f"Page {i + 1} summary",
                        "emotionalGoal": "determined",
                        "visualGoal": "wide shot",
                        "panelCount": 1,
                    }
                    for i in range(pc)
                ],
            })
        # Panel planner
        if "panels" in required:
            pc = _parse_panel_count(prompt)
            return json.dumps({
                "panels": [
                    {
                        "panelNumber": i + 1,
                        "purpose": f"panel {i + 1} shot",
                        "shot": "wide",
                        "cameraAngle": "low",
                        "action": "action",
                        "emotion": "determined",
                        "characters": ["char_hero"],
                        "background": "open field",
                        "visualPriority": "character",
                        "dialogue": [
                            {
                                "speaker": "Hero",
                                "text": "行くぞ",
                                "type": "speech",
                                "characterId": "char_hero",
                            }
                        ],
                    }
                    for i in range(pc)
                ],
            })
        # Prompt builder
        if "positive" in required:
            return json.dumps({
                "positive": "hero standing tall, wide shot, blue hair",
                "negative": "low quality, blurry",
                "seed": 12345,
                "width": 64,
                "height": 64,
            })
        return "{}"


# ---- FakeComfyClient ----

def _make_tiny_png_bytes(width: int = 64, height: int = 64) -> bytes:
    """Create a minimal valid PNG as bytes."""
    img = Image.new("RGB", (width, height), (100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeComfyClient:
    """Minimal ComfyClient stand-in for the submit → history → /view path."""

    def __init__(self) -> None:
        self.submit_calls: list[dict[str, Any]] = []
        self.history_calls: list[str] = []
        self.view_calls: list[dict[str, str]] = []
        self._prompt_id = "fake_prompt_001"
        self._output_filename = "ComfyUI_00001_.png"

    async def submit_workflow(
        self,
        graph: dict[str, Any],
        *,
        client_id: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> str:
        self.submit_calls.append({"graph": graph, "client_id": client_id})
        return self._prompt_id

    async def get_history(self, prompt_id: str | None = None) -> dict[str, Any]:
        self.history_calls.append(prompt_id or "")
        return {
            self._prompt_id: {
                "outputs": {
                    "3": {
                        "images": [
                            {
                                "filename": self._output_filename,
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                }
            }
        }

    async def fetch_view(
        self,
        filename: str,
        *,
        subfolder: str = "",
        type: str = "output",
    ) -> bytes:
        self.view_calls.append({"filename": filename, "subfolder": subfolder, "type": type})
        return _make_tiny_png_bytes()


# ---- Workflow payload ----

_WORKFLOW_PAYLOAD: dict[str, Any] = {
    "workflow_id": "anime_t2i_default",
    "name": "Test Text-to-Image",
    "type": "text_to_image",
    "file": "test_workflow.json",
    "description": "Minimal text_to_image workflow for E2E testing.",
    "api_graph": {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
                "seed": 0,
                "steps": 20,
                "cfg": 7.0,
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "model.safetensors"},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": ""},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": ""},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "ComfyUI"},
        },
    },
    "bindings": {
        "positive_prompt": {"node_id": "6", "input": "text"},
        "negative_prompt": {"node_id": "7", "input": "text"},
        "seed": {"node_id": "3", "input": "seed"},
        "width": {"node_id": "5", "input": "width"},
        "height": {"node_id": "5", "input": "height"},
        "output_node": {"node_id": "9", "input": "images"},
    },
}


# ---- Fixtures ----

@pytest.fixture()
async def comfy_e2e_client(aiohttp_client, tmp_path: Path):
    """E2E fixture: FakeLLMProvider + FakeComfyClient + WorkflowRegistry.

    Does NOT set ``manga_panel_executor_factory`` so the autopilot falls
    through to the ComfyExecutor path.
    """
    llm = FakeLLMProvider()
    fake_client = FakeComfyClient()

    # Build a real WorkflowRegistry and register the test workflow.
    from manga_autopilot.services.workflow_registry import WorkflowRegistry
    registry = WorkflowRegistry.open(tmp_path / "registry")
    registry.register(_WORKFLOW_PAYLOAD)

    app = web.Application()
    app["manga_llm_provider"] = llm
    app["manga_default_workflow_id"] = "anime_t2i_default"
    app["manga_comfy_client"] = fake_client
    app["manga_workflow_registry"] = registry
    # NOTE: deliberately NOT setting manga_panel_executor_factory

    register_all(app, storage_root=str(tmp_path))
    cli = await aiohttp_client(app)
    return cli, tmp_path, llm, fake_client, registry


# ---- Helpers ----

async def _wait_for_completion(cli, project_id: str, timeout: float = 15.0) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await cli.get(
            f"/manga_autopilot/api/projects/{project_id}/autopilot/status"
        )
        assert resp.status == 200
        body = await resp.json()
        last = body
        state = body.get("state", "")
        if state == "COMPLETED":
            return body
        if state in ("FAILED", "CANCELLED"):
            pytest.fail(f"autopilot reached terminal state: {state}")
        await asyncio.sleep(0.05)
    pytest.fail(f"autopilot did not complete within {timeout}s; last state={last.get('state')}")
    return last  # unreachable but satisfies type checker


# ---- Test ----

async def test_one_page_autopilot_uses_comfy_executor_path(comfy_e2e_client) -> None:
    """1-page / 1-panel autopilot through ComfyExecutor → FakeComfyClient."""
    cli, tmp_path, llm, fake_client, registry = comfy_e2e_client

    # 1. Create the project.
    create_resp = await cli.post(
        "/manga_autopilot/api/projects",
        json={"name": "ComfyExecutor E2E", "title": "ComfyExecutor"},
    )
    assert create_resp.status == 201
    project_id = (await create_resp.json())["id"]

    # 2. Start the autopilot.
    start_resp = await cli.post(
        f"/manga_autopilot/api/projects/{project_id}/autopilot/start",
        json={
            "idea": "A hero standing on a cliff",
            "page_count": 1,
            "panels_per_page": 1,
            "candidate_count": 1,
            "max_retries": 0,
        },
    )
    assert start_resp.status == 202

    # 3. Wait for completion.
    final = await _wait_for_completion(cli, project_id, timeout=20.0)
    assert final["state"] == "COMPLETED"

    project_root = tmp_path / "projects" / project_id

    # 4. ComfyClient methods were exercised.
    assert len(fake_client.submit_calls) == 1, (
        f"expected 1 submit_workflow call, got {len(fake_client.submit_calls)}"
    )
    assert len(fake_client.history_calls) >= 1
    assert len(fake_client.view_calls) >= 1

    # 5. Submitted graph has the overrides applied.
    submitted_graph = fake_client.submit_calls[0]["graph"]
    assert isinstance(submitted_graph, dict)
    # KSampler node (id "3") should have positive/seed from PromptSpec
    sampler = submitted_graph.get("3", {})
    sampler_inputs = sampler.get("inputs", {})
    assert "seed" in sampler_inputs
    # CLIPTextEncode positive (node "6") should have the positive prompt
    clip_pos = submitted_graph.get("6", {})
    clip_pos_inputs = clip_pos.get("inputs", {})
    assert clip_pos_inputs.get("text") == "hero standing tall, wide shot, blue hair"
    # CLIPTextEncode negative (node "7") should have the negative prompt
    clip_neg = submitted_graph.get("7", {})
    clip_neg_inputs = clip_neg.get("inputs", {})
    assert clip_neg_inputs.get("text") == "low quality, blurry"
    # EmptyLatentImage (node "5") should have width/height
    latent = submitted_graph.get("5", {})
    latent_inputs = latent.get("inputs", {})
    assert latent_inputs.get("width") == 64
    assert latent_inputs.get("height") == 64

    # 6. One panel record exists with an image.
    panels_path = project_root / "panels.json"
    assert panels_path.exists()
    panels = json.loads(panels_path.read_text(encoding="utf-8"))
    assert len(panels) == 1
    rec = panels[0]
    assert rec["image_path"] is not None
    assert Path(rec["image_path"]).exists()
    assert rec["status"] == "generated"
    assert rec["page_number"] == 1

    # 7. One job JSON exists and is completed.
    jobs_dir = project_root / "jobs"
    assert jobs_dir.is_dir()
    job_files = list(jobs_dir.iterdir())
    assert len(job_files) == 1
    job = json.loads(job_files[0].read_text(encoding="utf-8"))
    assert job["status"] == "completed"

    # 8. Page PNG was rendered.
    page_path = project_root / "exports" / "pages" / "page_0001.png"
    assert page_path.exists()
    assert page_path.stat().st_size > 0

    # 9. Bubbles exist.
    bubbles_path = project_root / "bubbles.json"
    assert bubbles_path.exists()
    bubbles = json.loads(bubbles_path.read_text(encoding="utf-8"))
    assert len(bubbles) >= 1
    assert bubbles[0]["text"]

    # 10. Manifest is correct.
    manifest_path = project_root / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project_id"] == project_id
    assert manifest["status"] == "completed"
    assert manifest["stats"]["page_count"] == 1
    assert manifest["stats"]["panel_count"] == 1
    assert manifest["stats"]["generated_images"] == 1

    # 11. generation_log.json confirms COMPLETED.
    log_path = project_root / "generation_log.json"
    assert log_path.exists()
    log_payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_payload["state"] == "COMPLETED"

    # 12. LLM was exercised for story/character/page/panel/prompt planning.
    assert len(llm.calls) >= 5
