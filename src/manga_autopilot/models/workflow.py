"""Workflow registry models + validation (spec section 12)."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

WORKFLOW_TYPES: tuple[str, ...] = (
    "text_to_image",
    "image_to_image",
    "reference_to_image",
    "character_sheet",
    "face_detail",
    "inpaint",
    "upscale",
    "background_only",
    "pose_control",
    "lineart_control",
)


class WorkflowType(str, Enum):
    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"
    REFERENCE_TO_IMAGE = "reference_to_image"
    CHARACTER_SHEET = "character_sheet"
    FACE_DETAIL = "face_detail"
    INPAINT = "inpaint"
    UPSCALE = "upscale"
    BACKGROUND_ONLY = "background_only"
    POSE_CONTROL = "pose_control"
    LINEART_CONTROL = "lineart_control"


WORKFLOW_ID_RE = re.compile(r"^[a-z0-9_\-]{1,64}$")


class WorkflowBinding(BaseModel):
    node_id: str
    input: str

    @field_validator("node_id", "input")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class WorkflowDefinition(BaseModel):
    workflow_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    type: WorkflowType | str
    file: str = Field(min_length=1, max_length=256)
    bindings: dict[str, WorkflowBinding] = Field(default_factory=dict)
    description: str | None = None
    api_graph: dict[str, Any] | None = None

    @field_validator("workflow_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not WORKFLOW_ID_RE.fullmatch(value):
            raise ValueError(
                "workflow_id must match ^[a-z0-9_-]{1,64}$ "
                "(lowercase alnum, underscore, dash)"
            )
        return value

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: object) -> object:
        if isinstance(value, WorkflowType):
            return value
        if value not in WORKFLOW_TYPES:
            raise ValueError(
                f"unsupported workflow type: {value!r}; must be one of {WORKFLOW_TYPES}"
            )
        return value

    # -- per-type required binding tables -------------------------------------
    # These are the canonical spec-12.3 binding requirements.  ``width`` /
    # ``height`` are required for workflows that synthesise pixels from
    # scratch; workflows that consume an existing image (upscale, face_detail,
    # inpaint) inherit dimensions from the input.
    _PROMPT_REQUIRED: frozenset[str] = frozenset({"text_to_image"})
    _REFERENCE_REQUIRED: frozenset[str] = frozenset(
        {"reference_to_image", "image_to_image", "inpaint", "face_detail", "upscale"}
    )
    _DIMENSION_REQUIRED: frozenset[str] = frozenset(
        {"text_to_image", "reference_to_image", "image_to_image", "background_only"}
    )

    def required_bindings(self) -> tuple[str, ...]:
        """Return the list of bindings required for this workflow type.

        The base set:

        - positive_prompt
        - negative_prompt
        - seed

        Plus, depending on the workflow type:

        - ``width`` and ``height`` for pixel-synthesising workflows
        - ``reference_image`` for workflows that consume an image

        For output selection, at least one of ``output_node`` or
        ``filename_prefix`` must be bound.  We expose the pair as
        :data:`OUTPUT_BINDING_ALTERNATIVES` so callers can special-case it.
        """

        wtype = self.type_value()
        parts: list[str] = ["positive_prompt", "negative_prompt", "seed"]
        if wtype in self._DIMENSION_REQUIRED:
            parts.extend(["width", "height"])
        if wtype in self._REFERENCE_REQUIRED:
            parts.append("reference_image")
        return tuple(parts)

    OUTPUT_BINDING_ALTERNATIVES: tuple[str, ...] = ("output_node", "filename_prefix")

    def has_output_binding(self) -> bool:
        """Return whether either output alternative is bound."""

        return any(key in self.bindings for key in self.OUTPUT_BINDING_ALTERNATIVES)

    def type_value(self) -> str:
        return self.type.value if isinstance(self.type, WorkflowType) else str(self.type)

    @model_validator(mode="after")
    def _ensure_required_bindings(self) -> WorkflowDefinition:
        required = set(self.required_bindings())
        present = set(self.bindings)
        missing = required - present
        if missing:
            raise ValueError(
                f"workflow is missing required bindings: {sorted(missing)}"
            )
        if not self.has_output_binding():
            raise ValueError(
                "workflow must bind at least one of: "
                + ", ".join(self.OUTPUT_BINDING_ALTERNATIVES)
            )
        return self


class WorkflowValidationError(ValueError):
    """Raised when a workflow fails structural validation."""


def validate_workflow_payload(payload: dict[str, Any]) -> WorkflowDefinition:
    """Parse and validate a raw workflow payload, returning a model.

    Raises :class:`WorkflowValidationError` on any structural problem.
    """

    if not isinstance(payload, dict):
        raise WorkflowValidationError("workflow payload must be a JSON object")
    try:
        return WorkflowDefinition.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError or ValueError
        raise WorkflowValidationError(str(exc)) from exc


def validate_api_graph(graph: Any) -> dict[str, Any]:
    """Validate the structure of a ComfyUI ``/prompt`` graph.

    The graph must be a mapping of node_id -> ``{"class_type": ..., "inputs": ...}``.
    """

    if not isinstance(graph, dict):
        raise WorkflowValidationError("api_graph must be a JSON object")
    cleaned: dict[str, Any] = {}
    for node_id, node in graph.items():
        if not isinstance(node_id, str) or not node_id:
            raise WorkflowValidationError(f"invalid node id: {node_id!r}")
        if not isinstance(node, dict):
            raise WorkflowValidationError(
                f"node {node_id!r} must be an object with class_type/inputs"
            )
        class_type = node.get("class_type")
        inputs = node.get("inputs", {})
        if not isinstance(class_type, str) or not class_type:
            raise WorkflowValidationError(
                f"node {node_id!r} is missing a non-empty class_type"
            )
        if not isinstance(inputs, dict):
            raise WorkflowValidationError(
                f"node {node_id!r} inputs must be a JSON object"
            )
        cleaned[node_id] = {"class_type": class_type, "inputs": inputs}
    return cleaned


def describe_binding_keys() -> tuple[str, ...]:
    """Return the canonical binding keys known to Manga Autopilot."""

    return (
        "positive_prompt",
        "negative_prompt",
        "seed",
        "steps",
        "cfg",
        "width",
        "height",
        "checkpoint",
        "filename_prefix",
        "output_node",
        "reference_image",
        "reference_strength",
        "ip_adapter_strength",
    )


def workflow_definition_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for :class:`WorkflowDefinition`."""

    return WorkflowDefinition.model_json_schema()


def workflow_binding_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for :class:`WorkflowBinding`."""

    return WorkflowBinding.model_json_schema()


def workflow_definition_schema_str(indent: int = 2) -> str:
    """Return the JSON Schema for :class:`WorkflowDefinition` as a string."""

    import json as _json

    return _json.dumps(workflow_definition_json_schema(), indent=indent, ensure_ascii=False)


def write_workflow_definition_schema(path: Path) -> Path:
    """Persist the JSON Schema for :class:`WorkflowDefinition` to ``path``."""

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(workflow_definition_schema_str(indent=2), encoding="utf-8")
    return dest


__all__ = [
    "WORKFLOW_TYPES",
    "WORKFLOW_ID_RE",
    "WorkflowType",
    "WorkflowBinding",
    "WorkflowDefinition",
    "WorkflowValidationError",
    "validate_workflow_payload",
    "validate_api_graph",
    "describe_binding_keys",
    "workflow_definition_json_schema",
    "workflow_binding_json_schema",
    "workflow_definition_schema_str",
    "write_workflow_definition_schema",
]
