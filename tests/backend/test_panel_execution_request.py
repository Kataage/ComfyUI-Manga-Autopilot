"""Tests for PanelExecutionRequest (issue #186).

Verifies that structured panel execution context is correctly built and
passed to all executors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from PIL import Image

from manga_autopilot.services.generation_job import (
    GenerationExecutorResult,
    GenerationLoop,
    GenerationLoopConfig,
    PanelExecutionRequest,
)
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.remote_executor import (
    FakeRemoteWorker,
    RemoteHTTPExecutor,
    RemoteWorkerSettings,
)

# ---------------------------------------------------------------- helpers

class FakeExecutor:
    """Records PanelExecutionRequest calls for assertions."""

    def __init__(self) -> None:
        self.calls: list[PanelExecutionRequest] = []

    async def submit(self, request: PanelExecutionRequest):
        self.calls.append(request)
        image = Image.new("RGB", (request.effective_width, request.effective_height), (request.seed % 256, 64, 200))
        return GenerationExecutorResult(
            candidate_id=request.candidate_id,
            prompt_id=f"prompt_{request.candidate_id}",
            image=image,
            workflow_id=request.workflow_id,
        )


def _parse_page_count(prompt: str) -> int:
    import re
    m = re.search(r"ページ数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


def _parse_panel_count(prompt: str) -> int:
    import re
    m = re.search(r"パネル数[：:は]\s*(\d+)", prompt)
    return int(m.group(1)) if m else 1


class FakeLLMProvider(LLMProvider):
    def __init__(self, settings: LLMSettings | None = None) -> None:
        super().__init__(settings or LLMSettings())
        self.calls: list[dict[str, Any]] = []

    async def complete(self, prompt: str, *, schema: dict[str, Any] | None = None, system: str | None = None) -> str:
        self.calls.append({"prompt": prompt})
        required = (schema or {}).get("required", [])
        if "title" in required and "pages" in required:
            pc = _parse_page_count(prompt)
            return json.dumps({
                "title": f"Sample {pc}-Page",
                "logline": "A hero adventure.",
                "genre": "fantasy",
                "pages": [{"pageNumber": i + 1, "summary": f"Page {i+1}", "emotionalGoal": "determined", "visualGoal": "wide shot", "panelCount": 1} for i in range(pc)],
            })
        if "characters" in required:
            return json.dumps({"characters": [{"id": "char_hero", "name": "Hero", "role": "protagonist", "visualTraits": ["blue hair"], "mustKeep": ["blue hair"], "styleHints": "manga"}]})
        if "pages" in required:
            pc = _parse_page_count(prompt)
            return json.dumps({"pages": [{"pageNumber": i + 1, "summary": f"Page {i+1}", "emotionalGoal": "determined", "visualGoal": "wide shot", "panelCount": 1} for i in range(pc)]})
        if "panels" in required:
            pc = _parse_panel_count(prompt)
            return json.dumps({"panels": [{"panelNumber": i + 1, "purpose": f"panel {i+1}", "shot": "wide", "cameraAngle": "low", "action": "action", "emotion": "determined", "characters": ["char_hero"], "background": "field", "visualPriority": "character", "dialogue": [{"speaker": "Hero", "text": "行くぞ", "type": "speech", "characterId": "char_hero"}]} for i in range(pc)]})
        if "positive" in required:
            return json.dumps({"positive": "hero standing, wide shot, blue hair", "negative": "low quality", "seed": 12345, "width": 64, "height": 64})
        return json.dumps({})


# ---------------------------------------------------------------- tests

def test_panel_execution_request_fields() -> None:
    """PanelExecutionRequest has all required fields."""
    prompt = PromptSpec(positive="test", negative="bad", seed=42, width=64, height=64, steps=4, cfg=7.0)
    req = PanelExecutionRequest(
        project_id="proj_001",
        page_id="page_0001",
        panel_id="panel_001",
        candidate_id="panel_001_c00",
        prompt=prompt,
        workflow_id="anime_t2i_default",
        seed=42,
        attempt_index=0,
    )
    assert req.project_id == "proj_001"
    assert req.page_id == "page_0001"
    assert req.panel_id == "panel_001"
    assert req.candidate_id == "panel_001_c00"
    assert req.prompt == prompt
    assert req.workflow_id == "anime_t2i_default"
    assert req.seed == 42
    assert req.attempt_index == 0


def test_panel_execution_request_effective_dimensions() -> None:
    """effective_width/height fall back to prompt dimensions."""
    prompt = PromptSpec(positive="test", negative="", seed=42, width=832, height=1216, steps=4, cfg=7.0)
    req = PanelExecutionRequest(
        project_id="proj_001", page_id="page_0001", panel_id="panel_001",
        candidate_id="c00", prompt=prompt, workflow_id="wf", seed=42,
    )
    assert req.effective_width == 832
    assert req.effective_height == 1216

    req2 = PanelExecutionRequest(
        project_id="proj_001", page_id="page_0001", panel_id="panel_001",
        candidate_id="c00", prompt=prompt, workflow_id="wf", seed=42,
        width=512, height=512,
    )
    assert req2.effective_width == 512
    assert req2.effective_height == 512


def test_panel_execution_request_frozen() -> None:
    """PanelExecutionRequest is frozen (immutable)."""
    prompt = PromptSpec(positive="test", negative="", seed=42, width=64, height=64, steps=4, cfg=7.0)
    req = PanelExecutionRequest(
        project_id="proj_001", page_id="page_0001", panel_id="panel_001",
        candidate_id="c00", prompt=prompt, workflow_id="wf", seed=42,
    )
    with pytest.raises(AttributeError):
        req.project_id = "changed"  # type: ignore[misc]


async def test_generation_loop_builds_panel_execution_request_context() -> None:
    """GenerationLoop passes project/page/panel/candidate context to executor."""
    import tempfile

    from manga_autopilot.models.page import PanelPlan

    executor = FakeExecutor()
    with tempfile.TemporaryDirectory() as tmp_dir:
        loop = GenerationLoop(
            project_root=Path(tmp_dir),
            config=GenerationLoopConfig(candidate_count=1, max_retries=0),
        )
        panel = PanelPlan(panel_number=1, purpose="test", shot="wide", camera_angle="low",
                          characters=[], background="field", action="act", emotion="determined",
                          visual_priority="character", dialogue=[], sfx=[])
        prompt = PromptSpec(positive="1girl", negative="bad", seed=42, width=64, height=64, steps=4, cfg=7.0)
        outcome = await loop.run(
            panel=panel,
            page_number=1,
            prompt=prompt,
            workflow_id="anime_t2i_default",
            executor=executor,
            project_id="proj_001",
        )
        assert outcome.job.status.value == "completed"
        assert len(executor.calls) == 1
        req = executor.calls[0]
        assert req.project_id == "proj_001"
        assert req.page_id == "page_0001"
        assert req.panel_id == "panel_001"
        assert req.candidate_id.startswith("panel_001_c")
        assert req.workflow_id == "anime_t2i_default"
        assert req.seed == 42
        assert req.effective_width == 64
        assert req.effective_height == 64


async def test_remote_executor_payload_includes_project_page_panel_and_candidate_ids() -> None:
    """RemoteHTTPExecutor sends project_id, page_id, panel_id, candidate_id in payload."""
    worker = FakeRemoteWorker(mode="success", seed=42)
    runner = web.AppRunner(worker.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None
    port = server.sockets[0].getsockname()[1]
    worker._server_port = port
    try:
        settings = RemoteWorkerSettings(base_url=f"http://127.0.0.1:{port}")
        executor = RemoteHTTPExecutor(settings=settings)
        prompt = PromptSpec(positive="test", negative="", seed=42, width=64, height=64, steps=4, cfg=7.0)
        request = PanelExecutionRequest(
            project_id="proj_001",
            page_id="page_0001",
            panel_id="panel_001",
            candidate_id="panel_001_c00",
            prompt=prompt,
            workflow_id="anime_t2i_default",
            seed=42,
            attempt_index=0,
        )
        result = await executor.submit(request)
        assert result.image is not None
        assert len(worker.requests) == 1
        payload = worker.requests[0]
        assert payload["project_id"] == "proj_001"
        assert payload["page_id"] == "page_0001"
        assert payload["panel_id"] == "panel_001"
        assert payload["candidate_id"] == "panel_001_c00"
        assert payload["workflow_id"] == "anime_t2i_default"
        assert payload["seed"] == 42
        assert payload["width"] == 64
        assert payload["height"] == 64
        assert payload["metadata"]["attempt_index"] == 0
    finally:
        await runner.cleanup()


def test_remote_generate_request_includes_candidate_id() -> None:
    """RemoteGenerateRequest.to_dict() includes candidate_id when set."""
    from manga_autopilot.services.remote_executor import RemoteGenerateRequest
    req = RemoteGenerateRequest(
        project_id="proj_001", page_id="page_0001", panel_id="panel_001",
        prompt="test", seed=42, width=64, height=64,
        candidate_id="panel_001_c00",
    )
    d = req.to_dict()
    assert d["candidate_id"] == "panel_001_c00"
    assert d["page_id"] == "page_0001"

    req2 = RemoteGenerateRequest(
        project_id="proj_001", page_id="page_0001", panel_id="panel_001",
        prompt="test", seed=42, width=64, height=64,
    )
    d2 = req2.to_dict()
    assert "candidate_id" not in d2
