"""Tests for workflow models and validation (spec section 12)."""

from __future__ import annotations

import pytest

from manga_autopilot.models.workflow import (
    WORKFLOW_ID_RE,
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowType,
    WorkflowValidationError,
    validate_api_graph,
    validate_workflow_payload,
)


def _valid_payload(**overrides) -> dict:
    payload = {
        "workflow_id": "anime_t2i_default",
        "name": "Anime T2I Default",
        "type": "text_to_image",
        "file": "workflows/anime_t2i_api.json",
        "bindings": {
            "positive_prompt": {"node_id": "6", "input": "text"},
            "negative_prompt": {"node_id": "7", "input": "text"},
            "seed": {"node_id": "3", "input": "seed"},
            "width": {"node_id": "5", "input": "width"},
            "height": {"node_id": "5", "input": "height"},
            "filename_prefix": {"node_id": "9", "input": "filename_prefix"},
        },
    }
    payload.update(overrides)
    return payload


def test_valid_payload_parses() -> None:
    wf = validate_workflow_payload(_valid_payload())
    assert isinstance(wf, WorkflowDefinition)
    assert wf.type_value() == "text_to_image"
    assert "positive_prompt" in wf.bindings


def test_workflow_id_regex() -> None:
    assert WORKFLOW_ID_RE.fullmatch("anime_t2i_default")
    assert not WORKFLOW_ID_RE.fullmatch("Anime_T2I")
    assert not WORKFLOW_ID_RE.fullmatch("with space")
    assert not WORKFLOW_ID_RE.fullmatch("")


def test_unknown_workflow_type_rejected() -> None:
    with pytest.raises(WorkflowValidationError):
        validate_workflow_payload(_valid_payload(type="nope"))


def test_missing_required_bindings_rejected() -> None:
    payload = _valid_payload()
    payload["bindings"] = {"seed": {"node_id": "3", "input": "seed"}}
    with pytest.raises(WorkflowValidationError):
        validate_workflow_payload(payload)


def test_reference_workflow_requires_reference_image() -> None:
    payload = _valid_payload(type="reference_to_image")
    payload["bindings"]["reference_image"] = {"node_id": "20", "input": "image"}
    wf = validate_workflow_payload(payload)
    assert "reference_image" in wf.bindings


def test_binding_validates_empty_fields() -> None:
    with pytest.raises(ValueError):
        WorkflowBinding(node_id="", input="text")


def test_validate_api_graph_accepts_comfyui_shape() -> None:
    graph = {
        "3": {"class_type": "KSampler", "inputs": {"seed": 42, "steps": 20}},
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "ComfyUI"},
        },
    }
    cleaned = validate_api_graph(graph)
    assert cleaned["3"]["class_type"] == "KSampler"
    assert cleaned["9"]["inputs"]["filename_prefix"] == "ComfyUI"


def test_validate_api_graph_rejects_non_dict_node() -> None:
    with pytest.raises(WorkflowValidationError):
        validate_api_graph({"3": "KSampler"})


def test_validate_api_graph_rejects_missing_class_type() -> None:
    with pytest.raises(WorkflowValidationError):
        validate_api_graph({"3": {"inputs": {}}})


def test_validate_api_graph_rejects_invalid_inputs() -> None:
    with pytest.raises(WorkflowValidationError):
        validate_api_graph({"3": {"class_type": "KSampler", "inputs": "nope"}})


def test_validate_workflow_payload_requires_dict() -> None:
    with pytest.raises(WorkflowValidationError):
        validate_workflow_payload([])  # type: ignore[arg-type]


def test_workflow_type_enum_coercion() -> None:
    wf = WorkflowDefinition(
        workflow_id="foo",
        name="Foo",
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
    assert wf.type_value() == "text_to_image"
