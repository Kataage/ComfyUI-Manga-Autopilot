"""Workflow execution / dispatcher (spec sections 12.4, 21.5, 23)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from manga_autopilot.models.workflow import (
    WorkflowDefinition,
    validate_api_graph,
)
from manga_autopilot.services.comfy_client import ComfyClient, ComfyUIError
from manga_autopilot.services.workflow_validator import (
    ValidationReport,
    validate_against_object_info,
)

log = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    ok: bool
    workflow_id: str
    prompt_id: str | None = None
    image_refs: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    report: ValidationReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "workflow_id": self.workflow_id,
            "prompt_id": self.prompt_id,
            "image_refs": list(self.image_refs),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "report": self.report.to_dict() if self.report is not None else None,
        }


def apply_overrides(
    graph: Mapping[str, Any],
    bindings: Mapping[str, Any],
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply ``overrides`` to ``graph`` using ``bindings`` to resolve node inputs.

    Each override key is the binding name (e.g. ``positive_prompt``); the
    value is the value to set on the resolved node input.
    """

    if not overrides:
        return {node_id: dict(node) for node_id, node in graph.items()}

    result: dict[str, Any] = {}
    for node_id, node in graph.items():
        result[node_id] = dict(node)
        if "inputs" not in result[node_id] or not isinstance(result[node_id]["inputs"], dict):
            result[node_id]["inputs"] = {}
        inputs = result[node_id]["inputs"]
        for key, binding in bindings.items():
            if binding.node_id != node_id:
                continue
            if key in overrides:
                inputs[binding.input] = overrides[key]
    return result


async def dispatch_test_run(
    workflow: WorkflowDefinition,
    api_graph: Mapping[str, Any] | None,
    *,
    client: ComfyClient,
    overrides: Mapping[str, Any] | None = None,
    object_info: Mapping[str, Any] | None = None,
) -> DispatchResult:
    """Validate, prepare, and submit a single test run to ComfyUI."""

    if api_graph is None:
        return DispatchResult(
            ok=False,
            workflow_id=workflow.workflow_id,
            errors=["api_graph is not embedded in the workflow definition"],
        )

    try:
        graph = validate_api_graph(dict(api_graph))
    except Exception as exc:  # WorkflowValidationError (ValueError)
        return DispatchResult(
            ok=False,
            workflow_id=workflow.workflow_id,
            errors=[f"invalid api_graph: {exc}"],
        )

    if object_info is not None:
        report = validate_against_object_info(workflow, graph, object_info)
        if not report.ok:
            return DispatchResult(
                ok=False,
                workflow_id=workflow.workflow_id,
                errors=report.errors,
                warnings=report.warnings,
                report=report,
            )
    else:
        report = None

    prepared = apply_overrides(graph, workflow.bindings, overrides)
    try:
        prompt_id = await client.submit_workflow(prepared)
    except ComfyUIError as exc:
        return DispatchResult(
            ok=False,
            workflow_id=workflow.workflow_id,
            errors=[f"submit failed: {exc}"],
            report=report,
        )

    image_refs: list[dict[str, Any]] = []
    try:
        history = await client.get_history(prompt_id)
        entry = history.get(prompt_id, {}) if isinstance(history, dict) else {}
        if isinstance(entry, dict):
            image_refs = ComfyClient.extract_output_images(entry)
    except ComfyUIError as exc:
        log.warning("history fetch failed for %s: %s", prompt_id, exc)

    return DispatchResult(
        ok=True,
        workflow_id=workflow.workflow_id,
        prompt_id=prompt_id,
        image_refs=image_refs,
        warnings=report.warnings if report is not None else [],
        report=report,
    )


__all__ = ["DispatchResult", "apply_overrides", "dispatch_test_run"]
