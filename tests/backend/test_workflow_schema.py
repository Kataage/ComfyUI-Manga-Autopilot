"""Tests for JSON schema export of the workflow definition model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manga_autopilot.models.workflow import (
    WORKFLOW_TYPES,
    workflow_binding_json_schema,
    workflow_definition_json_schema,
    workflow_definition_schema_str,
    write_workflow_definition_schema,
)


def test_workflow_definition_json_schema_shape() -> None:
    schema = workflow_definition_json_schema()
    assert schema["type"] == "object"
    props = schema["properties"]
    assert {"workflow_id", "name", "type", "file", "bindings"} <= set(props)
    # Pydantic emits $defs for the WorkflowType enum; resolve the $ref.
    type_prop = props["type"]
    defs = schema.get("$defs", {})
    enum_def = defs.get("WorkflowType", {})
    assert "enum" in enum_def, f"expected WorkflowType enum in $defs, got {type_prop!r}"
    assert set(enum_def["enum"]) == set(WORKFLOW_TYPES)


def test_workflow_binding_json_schema_shape() -> None:
    schema = workflow_binding_json_schema()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"node_id", "input"}


def test_workflow_definition_schema_str_is_valid_json() -> None:
    text = workflow_definition_schema_str()
    parsed = json.loads(text)
    assert parsed["type"] == "object"


def test_write_workflow_definition_schema(tmp_path: Path) -> None:
    out = tmp_path / "schemas" / "workflow_definition.schema.json"
    result = write_workflow_definition_schema(out)
    assert result == out
    assert out.exists()
    payload = json.loads(out.read_text("utf-8"))
    assert payload["type"] == "object"


def test_required_bindings_per_type_enforced_by_validator() -> None:
    from manga_autopilot.models.workflow import (
        WorkflowDefinition,
        WorkflowValidationError,
        validate_workflow_payload,
    )

    # text_to_image without output bindings should fail.
    base = {
        "workflow_id": "t2i",
        "name": "T2I",
        "file": "wf.json",
        "type": "text_to_image",
        "bindings": {
            "positive_prompt": {"node_id": "6", "input": "text"},
        },
    }
    with pytest.raises(WorkflowValidationError):
        validate_workflow_payload(base)

    # reference_to_image with all required bindings succeeds.
    full = {
        "workflow_id": "rt",
        "name": "RT",
        "file": "wf.json",
        "type": "reference_to_image",
        "bindings": {
            "positive_prompt": {"node_id": "6", "input": "text"},
            "negative_prompt": {"node_id": "7", "input": "text"},
            "seed": {"node_id": "3", "input": "seed"},
            "width": {"node_id": "5", "input": "width"},
            "height": {"node_id": "5", "input": "height"},
            "filename_prefix": {"node_id": "9", "input": "filename_prefix"},
            "reference_image": {"node_id": "20", "input": "image"},
        },
    }
    wf = validate_workflow_payload(full)
    assert isinstance(wf, WorkflowDefinition)
    assert "reference_image" in wf.required_bindings()
