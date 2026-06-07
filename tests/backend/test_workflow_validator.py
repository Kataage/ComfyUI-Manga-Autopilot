"""Tests for the workflow validator (spec section 12.4)."""

from __future__ import annotations

from manga_autopilot.models.workflow import (
    WorkflowDefinition,
    WorkflowType,
)
from manga_autopilot.services.workflow_validator import (
    ValidationReport,
    validate_against_object_info,
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
        "3": {
            "class_type": "KSampler",
            "inputs": {"seed": 1, "steps": 20},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "a cat"},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "lowres"},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "ComfyUI"},
        },
    }


def _object_info() -> dict:
    return {
        "KSampler": {
            "input": {"required": {"seed": ("INT",), "steps": ("INT",)}}
        },
        "EmptyLatentImage": {
            "input": {"required": {"width": ("INT",), "height": ("INT",)}}
        },
        "CLIPTextEncode": {"input": {"required": {"text": ("STRING",)}}},
        "SaveImage": {"input": {"required": {"filename_prefix": ("STRING",)}}},
    }


def test_valid_workflow_passes() -> None:
    report = validate_against_object_info(_workflow(), _graph(), _object_info())
    assert report.ok is True
    assert report.errors == []


def test_missing_class_reported() -> None:
    graph = _graph()
    graph["3"]["class_type"] = "UnknownKSampler"
    report = validate_against_object_info(_workflow(), graph, _object_info())
    assert report.ok is False
    assert "UnknownKSampler" in report.missing_classes


def test_missing_node_id_reported() -> None:
    graph = _graph()
    del graph["3"]
    report = validate_against_object_info(_workflow(), graph, _object_info())
    assert report.ok is False
    assert "3" in report.missing_nodes


def test_missing_input_reported() -> None:
    wf = _workflow()
    wf.bindings["seed"].input = "notaseed"
    report = validate_against_object_info(wf, _graph(), _object_info())
    assert report.ok is False
    assert any("3.notaseed" in m for m in report.missing_inputs)


def test_graph_shape_error_reported() -> None:
    graph = _graph()
    graph["3"] = "not an object"
    report = validate_against_object_info(_workflow(), graph, _object_info())
    assert report.ok is False
    assert any("must be an object" in e for e in report.errors)


def test_missing_api_graph_warns_only() -> None:
    report = validate_against_object_info(_workflow(), None, _object_info())
    assert report.ok is True
    assert report.warnings


def test_required_binding_missing_reports_error() -> None:
    wf = _workflow()
    del wf.bindings["seed"]
    report = validate_against_object_info(wf, _graph(), _object_info())
    assert report.ok is False
    assert any("seed" in e for e in report.errors)


def test_report_serialisation() -> None:
    report = ValidationReport(ok=True)
    data = report.to_dict()
    assert set(data) == {
        "ok",
        "errors",
        "warnings",
        "missing_nodes",
        "missing_inputs",
        "missing_classes",
    }
