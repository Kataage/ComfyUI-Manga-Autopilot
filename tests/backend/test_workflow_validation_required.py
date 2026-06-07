"""Tests for the required-binding validation on WorkflowDefinition."""

from __future__ import annotations

import pytest

from manga_autopilot.models.workflow import (
    WorkflowDefinition,
    WorkflowValidationError,
    validate_workflow_payload,
)


def _binding(node: str, input_: str) -> dict[str, str]:
    return {"node_id": node, "input": input_}


def _base_payload(**overrides):
    payload = {
        "workflow_id": "wf",
        "name": "WF",
        "type": "text_to_image",
        "file": "wf.json",
        "bindings": {
            "positive_prompt": _binding("6", "text"),
            "negative_prompt": _binding("7", "text"),
            "seed": _binding("3", "seed"),
            "width": _binding("5", "width"),
            "height": _binding("5", "height"),
            "filename_prefix": _binding("9", "filename_prefix"),
        },
    }
    payload.update(overrides)
    return payload


def test_required_bindings_accepts_text_to_image_with_filename_prefix():
    wf = WorkflowDefinition.model_validate(_base_payload())
    assert set(wf.required_bindings()) == {
        "positive_prompt", "negative_prompt", "seed", "width", "height",
    }
    assert wf.has_output_binding() is True


def test_required_bindings_rejects_missing_positive_prompt():
    payload = _base_payload()
    del payload["bindings"]["positive_prompt"]
    with pytest.raises(WorkflowValidationError, match="positive_prompt"):
        validate_workflow_payload(payload)


def test_required_bindings_rejects_missing_negative_prompt():
    payload = _base_payload()
    del payload["bindings"]["negative_prompt"]
    with pytest.raises(WorkflowValidationError, match="negative_prompt"):
        validate_workflow_payload(payload)


def test_required_bindings_rejects_missing_seed():
    payload = _base_payload()
    del payload["bindings"]["seed"]
    with pytest.raises(WorkflowValidationError, match="seed"):
        validate_workflow_payload(payload)


def test_required_bindings_rejects_missing_dimensions():
    payload = _base_payload()
    del payload["bindings"]["width"]
    with pytest.raises(WorkflowValidationError, match="width"):
        validate_workflow_payload(payload)
    payload = _base_payload()
    del payload["bindings"]["height"]
    with pytest.raises(WorkflowValidationError, match="height"):
        validate_workflow_payload(payload)


def test_required_bindings_rejects_neither_output():
    payload = _base_payload()
    del payload["bindings"]["filename_prefix"]
    with pytest.raises(WorkflowValidationError, match="output_node"):
        validate_workflow_payload(payload)


def test_required_bindings_accepts_output_node_instead_of_filename_prefix():
    payload = _base_payload(bindings={
        "positive_prompt": _binding("6", "text"),
        "negative_prompt": _binding("7", "text"),
        "seed": _binding("3", "seed"),
        "width": _binding("5", "width"),
        "height": _binding("5", "height"),
        "output_node": _binding("9", "_dummy"),
    })
    wf = WorkflowDefinition.model_validate(payload)
    assert wf.has_output_binding() is True


def test_upscale_requires_reference_image_not_dimensions():
    payload = {
        "workflow_id": "up",
        "name": "Upscale",
        "type": "upscale",
        "file": "up.json",
        "bindings": {
            "positive_prompt": _binding("6", "text"),
            "negative_prompt": _binding("7", "text"),
            "seed": _binding("3", "seed"),
            "reference_image": _binding("10", "image"),
            "filename_prefix": _binding("9", "filename_prefix"),
        },
    }
    wf = WorkflowDefinition.model_validate(payload)
    assert "reference_image" in wf.required_bindings()
    assert "width" not in wf.required_bindings()
    assert "height" not in wf.required_bindings()


def test_reference_to_image_requires_reference_image():
    payload = {
        "workflow_id": "ref",
        "name": "Ref",
        "type": "reference_to_image",
        "file": "ref.json",
        "bindings": {
            "positive_prompt": _binding("6", "text"),
            "negative_prompt": _binding("7", "text"),
            "seed": _binding("3", "seed"),
            "width": _binding("5", "width"),
            "height": _binding("5", "height"),
            "filename_prefix": _binding("9", "filename_prefix"),
        },
    }
    with pytest.raises(WorkflowValidationError, match="reference_image"):
        validate_workflow_payload(payload)
