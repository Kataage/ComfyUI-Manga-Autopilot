"""JSON schema validation utility (spec sections 14.7 and 23.4)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, ValidationError, model_validator

log = logging.getLogger(__name__)


@dataclass
class SchemaError:
    path: str
    message: str


@dataclass
class ValidationOutcome:
    ok: bool
    errors: list[SchemaError] = field(default_factory=list)
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [{"path": e.path, "message": e.message} for e in self.errors],
        }


class JsonSchemaValidator:
    """Wraps :mod:`jsonschema` for the spec's LLM-output validation use case."""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        # Force evaluation so the schema is valid up-front.
        Draft202012Validator.check_schema(dict(schema))
        self._validator = Draft202012Validator(dict(schema))

    def validate(self, payload: Any) -> ValidationOutcome:
        errors: list[SchemaError] = []
        for err in self._validator.iter_errors(payload):
            errors.append(
                SchemaError(
                    path="/" + "/".join(str(p) for p in err.absolute_path) if err.absolute_path else "/",
                    message=err.message,
                )
            )
        return ValidationOutcome(ok=not errors, errors=errors, data=payload if not errors else None)


def validate_llm_output(
    payload: Any,
    *,
    jsonschema_definition: Mapping[str, Any] | None = None,
    pydantic_model: type[BaseModel] | None = None,
) -> ValidationOutcome:
    """Validate ``payload`` against a JSON schema and/or a Pydantic model.

    Either ``jsonschema_definition`` or ``pydantic_model`` must be supplied.
    When both are provided, the payload must satisfy both.
    """

    if jsonschema_definition is None and pydantic_model is None:
        raise ValueError("at least one of jsonschema_definition/pydantic_model is required")

    if jsonschema_definition is not None:
        outcome = JsonSchemaValidator(jsonschema_definition).validate(payload)
        if not outcome.ok:
            return outcome

    if pydantic_model is not None:
        try:
            pydantic_model.model_validate(payload)
        except ValidationError as exc:
            return ValidationOutcome(
                ok=False,
                errors=[
                    SchemaError(
                        path="/" + "/".join(str(p) for p in e["loc"]),
                        message=e["msg"],
                    )
                    for e in exc.errors()
                ],
            )
    return ValidationOutcome(ok=True, data=payload)


class ValidatedJsonSchema(BaseModel):
    """Pydantic wrapper that lets callers embed a JSON schema as a field."""

    schema_: dict[str, Any] = Field(alias="schema")

    @model_validator(mode="after")
    def _validate_schema(self) -> ValidatedJsonSchema:
        Draft202012Validator.check_schema(self.schema_)
        return self


__all__ = [
    "SchemaError",
    "ValidationOutcome",
    "JsonSchemaValidator",
    "validate_llm_output",
    "ValidatedJsonSchema",
]
