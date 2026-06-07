"""Validate registered workflows against ComfyUI's ``/object_info`` (spec 12.4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from manga_autopilot.models.workflow import (
    WorkflowDefinition,
    validate_api_graph,
)


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_nodes: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    missing_classes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "missing_nodes": list(self.missing_nodes),
            "missing_inputs": list(self.missing_inputs),
            "missing_classes": list(self.missing_classes),
        }


def validate_against_object_info(
    workflow: WorkflowDefinition,
    api_graph: Mapping[str, Any] | None,
    object_info: Mapping[str, Any],
) -> ValidationReport:
    """Validate ``workflow`` bindings + ``api_graph`` against ``object_info``.

    ``object_info`` is the dict returned by ComfyUI's ``/object_info`` endpoint
    (class name -> info).  The function reports missing nodes, missing
    class_types, and binding inputs that don't exist on the resolved node.
    """

    report = ValidationReport(ok=True)

    # 1) Validate api_graph shape.
    graph: dict[str, dict[str, Any]] = {}
    if api_graph is None:
        report.warnings.append("api_graph is not embedded in the workflow definition")
        return report
    try:
        graph = validate_api_graph(dict(api_graph))
    except Exception as exc:  # WorkflowValidationError is a ValueError
        report.errors.append(str(exc))
        report.ok = False
        return report

    # 2) class_type existence.
    for node_id, node in graph.items():
        class_type = node["class_type"]
        if class_type not in object_info:
            report.missing_classes.append(class_type)
            report.errors.append(
                f"node {node_id!r} uses class_type {class_type!r} which is not in /object_info"
            )

    # 3) bindings -> node existence + input existence.
    for key, binding in workflow.bindings.items():
        node_id = binding.node_id
        if node_id not in graph:
            report.missing_nodes.append(node_id)
            report.errors.append(
                f"binding {key!r} targets node {node_id!r} which is missing from api_graph"
            )
            continue
        class_info = object_info.get(graph[node_id]["class_type"], {})
        if not isinstance(class_info, dict):
            continue
        inputs_def = class_info.get("input", {})
        if not isinstance(inputs_def, dict):
            continue
        required = inputs_def.get("required", {}) or {}
        optional = inputs_def.get("optional", {}) or {}
        available = set(required) | set(optional)
        if available and binding.input not in available:
            report.missing_inputs.append(f"{node_id}.{binding.input}")
            report.errors.append(
                f"binding {key!r} targets input {binding.input!r} on node {node_id!r} "
                f"({graph[node_id]['class_type']!r}), which is not declared"
            )

    # 4) required bindings present.
    required_keys = set(workflow.required_bindings())
    present_keys = set(workflow.bindings)
    missing = required_keys - present_keys
    if missing:
        # ``output_node`` may be substituted by ``filename_prefix`` (see spec 12.3)
        if not (missing <= {"output_node", "filename_prefix"}):
            for key in sorted(missing):
                report.errors.append(f"required binding {key!r} is not configured")
        elif "filename_prefix" not in present_keys and "output_node" not in present_keys:
            report.errors.append("neither output_node nor filename_prefix is bound")

    if report.errors:
        report.ok = False
    return report


__all__ = ["ValidationReport", "validate_against_object_info"]
