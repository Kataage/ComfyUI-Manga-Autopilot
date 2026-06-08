"""Validate example workflow registry payload (issue #174).

Verifies that the example registry JSON in ``examples/workflows/``
is structurally valid, can be registered in a
:class:`WorkflowRegistry`, and works with the ComfyExecutor +
FakeComfyClient E2E path.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from manga_autopilot.models.workflow import validate_workflow_payload
from manga_autopilot.services.generation_job import ComfyExecutor, GenerationExecutorResult
from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.workflow_registry import WorkflowRegistry

_EXAMPLE_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "workflows"
_REGISTRY_PAYLOAD = _EXAMPLE_DIR / "anime_t2i_default.registry.json"
_WORKFLOW_GRAPH = _EXAMPLE_DIR / "anime_t2i_default.workflow.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeComfyClient:
    """Minimal fake that captures the submitted graph."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit_workflow(self, graph: dict[str, Any]) -> str:
        self.submitted.append(graph)
        return "fake_prompt_001"

    async def get_history(self, prompt_id: str) -> dict[str, Any]:
        return {
            prompt_id: {
                "outputs": {
                    "9": {"images": [{"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}]}
                }
            }
        }

    async def fetch_view(self, filename: str, subfolder: str = "", type: str = "output") -> bytes:
        img = Image.new("RGB", (64, 64), (128, 64, 200))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_example_registry_payload_exists() -> None:
    """Registry payload file exists and is non-empty."""
    assert _REGISTRY_PAYLOAD.exists(), f"missing: {_REGISTRY_PAYLOAD}"
    assert _REGISTRY_PAYLOAD.stat().st_size > 0


def test_example_registry_payload_is_valid_json() -> None:
    """Registry payload is valid JSON."""
    data = json.loads(_REGISTRY_PAYLOAD.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_example_registry_payload_matches_workflow_definition() -> None:
    """Registry payload validates against WorkflowDefinition schema."""
    data = json.loads(_REGISTRY_PAYLOAD.read_text(encoding="utf-8"))
    wf = validate_workflow_payload(data)
    assert wf.workflow_id == "anime_t2i_default"
    assert wf.type == "text_to_image"
    assert wf.api_graph is not None
    assert len(wf.api_graph) > 0


def test_example_registry_has_required_bindings() -> None:
    """Registry payload has all required bindings for text_to_image."""
    data = json.loads(_REGISTRY_PAYLOAD.read_text(encoding="utf-8"))
    wf = validate_workflow_payload(data)
    required = {"positive_prompt", "negative_prompt", "seed", "width", "height"}
    assert required.issubset(set(wf.bindings.keys())), (
        f"missing bindings: {required - set(wf.bindings.keys())}"
    )
    assert wf.has_output_binding(), "must bind output_node or filename_prefix"


def test_example_workflow_graph_exists() -> None:
    """Standalone workflow graph file exists and is valid JSON."""
    assert _WORKFLOW_GRAPH.exists(), f"missing: {_WORKFLOW_GRAPH}"
    graph = json.loads(_WORKFLOW_GRAPH.read_text(encoding="utf-8"))
    assert isinstance(graph, dict)
    assert len(graph) > 0
    for node_id, node in graph.items():
        assert "class_type" in node, f"node {node_id} missing class_type"
        assert "inputs" in node, f"node {node_id} missing inputs"


def test_example_registry_can_be_registered(tmp_path: Path) -> None:
    """Registry payload can be registered in a WorkflowRegistry."""
    data = json.loads(_REGISTRY_PAYLOAD.read_text(encoding="utf-8"))
    registry = WorkflowRegistry.open(tmp_path / "registry")
    wf = registry.register(data)
    assert wf.workflow_id == "anime_t2i_default"

    retrieved = registry.get("anime_t2i_default")
    assert retrieved is not None
    assert retrieved.workflow_id == "anime_t2i_default"
    assert retrieved.api_graph is not None


async def test_example_registry_comfy_executor_e2e(tmp_path: Path) -> None:
    """Example workflow works with ComfyExecutor + FakeComfyClient."""
    data = json.loads(_REGISTRY_PAYLOAD.read_text(encoding="utf-8"))
    registry = WorkflowRegistry.open(tmp_path / "registry")
    registry.register(data)

    client = _FakeComfyClient()
    executor = ComfyExecutor(client=client, registry=registry)

    prompt = PromptSpec(
        positive="1girl, masterpiece",
        negative="lowres, bad anatomy",
        seed=42,
        width=832,
        height=1216,
        steps=28,
        cfg=7.0,
    )

    outcome = await executor.submit(
        prompt=prompt,
        workflow_id="anime_t2i_default",
        seed=42,
        candidate_id="cand_001",
    )
    assert isinstance(outcome, GenerationExecutorResult)
    assert outcome.candidate_id == "cand_001"
    assert outcome.image is not None

    assert len(client.submitted) == 1
    submitted_graph = client.submitted[0]
    assert submitted_graph["6"]["inputs"]["text"] == "1girl, masterpiece"
    assert submitted_graph["7"]["inputs"]["text"] == "lowres, bad anatomy"
    assert submitted_graph["3"]["inputs"]["seed"] == 42
    assert submitted_graph["5"]["inputs"]["width"] == 832
    assert submitted_graph["5"]["inputs"]["height"] == 1216
