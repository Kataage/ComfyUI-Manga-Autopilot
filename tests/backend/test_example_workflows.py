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
from manga_autopilot.services.generation_job import (
    ComfyExecutor,
    GenerationExecutorResult,
    PanelExecutionRequest,
)
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

    outcome = await executor.submit(PanelExecutionRequest(
        project_id="proj_001",
        page_id="page_0001",
        panel_id="panel_001",
        candidate_id="cand_001",
        prompt=prompt,
        workflow_id="anime_t2i_default",
        seed=42,
    ))
    assert isinstance(outcome, GenerationExecutorResult)
    assert outcome.candidate_id == "cand_001"
    assert outcome.image is not None

    assert len(client.submitted) == 1
    submitted_graph = client.submitted[0]
    assert submitted_graph["6"]["inputs"]["text"] == "1girl, masterpiece"
    # The negative reaching the sampler is negative_full(): the prompt's own
    # negatives plus the application's text/watermark bans.
    negative = submitted_graph["7"]["inputs"]["text"]
    assert negative.endswith("lowres, bad anatomy")
    assert "watermark" in negative
    assert submitted_graph["3"]["inputs"]["seed"] == 42
    assert submitted_graph["5"]["inputs"]["width"] == 832
    assert submitted_graph["5"]["inputs"]["height"] == 1216


# ---------------------------------------------------------------------------
# Anima Turbo example (plan Task 8, step 4)
# ---------------------------------------------------------------------------

_ANIMA_REGISTRY = _EXAMPLE_DIR / "anima_turbo.registry.json"
_ANIMA_GRAPH = _EXAMPLE_DIR / "anima_turbo.workflow.json"


def _anima_payload() -> dict[str, Any]:
    return json.loads(_ANIMA_REGISTRY.read_text(encoding="utf-8"))


def test_anima_example_is_a_valid_workflow_payload() -> None:
    definition = validate_workflow_payload(_anima_payload())

    assert definition.workflow_id == "anima_turbo"
    assert definition.type_value() == "text_to_image"
    assert definition.has_output_binding()


def test_anima_example_graph_matches_the_standalone_file() -> None:
    payload = _anima_payload()

    assert payload["api_graph"] == json.loads(_ANIMA_GRAPH.read_text(encoding="utf-8"))


def test_anima_example_bindings_all_resolve() -> None:
    payload = _anima_payload()
    graph = payload["api_graph"]

    for key, binding in payload["bindings"].items():
        node = graph.get(binding["node_id"])
        assert node is not None, f"{key} points at a missing node"
        assert binding["input"] in node["inputs"], f"{key} points at a missing input"


def test_anima_example_keeps_the_verified_models_and_sampling() -> None:
    graph = _anima_payload()["api_graph"]
    by_class = {node["class_type"]: node["inputs"] for node in graph.values()}

    assert by_class["UNETLoader"]["unet_name"] == "silvermoonmixAnima_v23.safetensors"
    assert by_class["CLIPLoader"]["clip_name"] == "qwen_3_06b_base.safetensors"
    assert by_class["VAELoader"]["vae_name"] == "qwen_image_vae.safetensors"
    assert by_class["LoraLoader|pysssss"]["lora_name"] == "anima-turbo-lora-v0.2.safetensors"
    sampler = by_class["KSampler"]
    assert (sampler["steps"], sampler["cfg"]) == (12, 1.0)
    assert (sampler["sampler_name"], sampler["scheduler"]) == ("er_sde", "simple")


def test_anima_example_carries_no_user_specific_prompt_text() -> None:
    raw = _ANIMA_GRAPH.read_text(encoding="utf-8")

    # The source workflow's positive prompt described a specific person and outfit.
    for leaked in ("navy pleated skirt", "white blouse", "anima_two_prompt"):
        assert leaked not in raw


def test_anima_profile_overrides_the_example_generation_fields() -> None:
    from manga_autopilot.models.generation_profile import SemanticPromptSegments
    from manga_autopilot.services.anima_prompt_builder import AnimaPromptBuilder
    from manga_autopilot.services.generation_profiles import load_builtin_profile

    graph = _anima_payload()["api_graph"]
    baked = graph["8"]["inputs"]
    profile = load_builtin_profile("anima_turbo")

    spec = AnimaPromptBuilder().render(
        SemanticPromptSegments(subject=["1girl"]),
        profile,
        seed=999,
        panel_size=(3, 4),
    )

    # The profile decides; the values baked into the example are only defaults.
    assert (spec.steps, spec.cfg) == (baked["steps"], baked["cfg"])
    assert (spec.sampler, spec.scheduler) == (baked["sampler_name"], baked["scheduler"])
    assert spec.seed == 999 != baked["seed"]
    assert (spec.width, spec.height) == (
        graph["7"]["inputs"]["width"],
        graph["7"]["inputs"]["height"],
    )


def test_anima_example_registers_in_a_registry(tmp_path: Path) -> None:
    registry = WorkflowRegistry.open(tmp_path)

    definition = registry.register(_anima_payload())

    assert registry.get("anima_turbo").workflow_id == definition.workflow_id


def test_anima_example_passes_preflight_against_its_own_node_set(tmp_path: Path) -> None:
    from manga_autopilot.services.generation_profiles import load_builtin_profile
    from manga_autopilot.services.preflight import (
        AnimaPreflight,
        ComfyCapabilities,
        PreflightRequest,
    )

    graph = _anima_payload()["api_graph"]
    # A capability snapshot describing exactly what this example needs.
    object_info = {
        node["class_type"]: {"input": {"required": {}}} for node in graph.values()
    }
    object_info["UNETLoader"]["input"]["required"]["unet_name"] = [
        ["silvermoonmixAnima_v23.safetensors"],
        {},
    ]
    object_info["CLIPLoader"]["input"]["required"]["clip_name"] = [
        ["qwen_3_06b_base.safetensors"],
        {},
    ]
    object_info["VAELoader"]["input"]["required"]["vae_name"] = [
        ["qwen_image_vae.safetensors"],
        {},
    ]
    object_info["LoraLoader|pysssss"]["input"]["required"]["lora_name"] = [
        ["anima-turbo-lora-v0.2.safetensors"],
        {},
    ]

    report = AnimaPreflight(ComfyCapabilities.from_object_info(object_info)).run(
        PreflightRequest(
            profile=load_builtin_profile("anima_turbo"),
            workflow=validate_workflow_payload(_anima_payload()),
            output_dir=tmp_path / "out",
            license_acknowledged=True,
            panel_sizes=[(3, 4)],
        )
    )

    assert report.errors == ()


async def test_executor_sends_the_application_bans_in_the_negative_prompt(
    tmp_path: Path,
) -> None:
    """The text/watermark bans must reach the sampler, not just the snapshot.

    ``PromptSpec.negative_full()`` is the documented "what the sampler sees"
    accessor and the run snapshot records it, so the executor has to send it.
    Sending bare ``negative`` dropped the bans and made snapshots disagree with
    what was actually rendered.
    """
    from manga_autopilot.services.prompt_builder import MANGA_NEGATIVE

    registry = WorkflowRegistry.open(tmp_path)
    registry.register(_anima_payload())
    client = _FakeComfyClient()
    executor = ComfyExecutor(client=client, registry=registry, workflow_id="anima_turbo")

    await executor.submit(
        PanelExecutionRequest(
            project_id="p1",
            page_id="page_0001",
            panel_id="p1_01",
            candidate_id="p1_01_c00",
            prompt=PromptSpec(
                positive="1girl", negative="extra arms", seed=1, width=960, height=1280
            ),
            workflow_id="anima_turbo",
            seed=1,
            width=960,
            height=1280,
        )
    )

    negative = client.submitted[-1]["6"]["inputs"]["text"]
    assert "extra arms" in negative
    assert "watermark" in negative
    assert "speech text in image" in negative
    assert "speech text in image" in MANGA_NEGATIVE
