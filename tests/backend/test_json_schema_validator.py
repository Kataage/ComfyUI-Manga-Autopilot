"""Tests for the JSON schema validation utility (spec sections 14.7, 23.4)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from manga_autopilot.services.json_schema_validator import (
    JsonSchemaValidator,
    SchemaError,
    ValidationOutcome,
    validate_llm_output,
)


def test_jsonschema_validator_accepts_valid_payload() -> None:
    schema = {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    validator = JsonSchemaValidator(schema)
    outcome = validator.validate({"title": "Hi"})
    assert outcome.ok
    assert outcome.errors == []


def test_jsonschema_validator_reports_missing_keys() -> None:
    schema = {
        "type": "object",
        "required": ["title", "synopsis"],
        "properties": {
            "title": {"type": "string"},
            "synopsis": {"type": "string"},
        },
    }
    outcome = JsonSchemaValidator(schema).validate({"title": "Hi"})
    assert not outcome.ok
    assert any("synopsis" in e.message for e in outcome.errors)


def test_jsonschema_validator_nested_path() -> None:
    schema = {
        "type": "object",
        "required": ["pages"],
        "properties": {
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pageNumber"],
                    "properties": {"pageNumber": {"type": "integer"}},
                },
            }
        },
    }
    outcome = JsonSchemaValidator(schema).validate(
        {"pages": [{"pageNumber": 1}, {"synopsis": "bad"}]}
    )
    assert not outcome.ok
    assert outcome.errors[0].path.startswith("/pages/")


def test_jsonschema_validator_invalid_schema() -> None:
    from jsonschema import SchemaError as JSchemaError

    with pytest.raises(JSchemaError):
        JsonSchemaValidator({"type": "not-a-type"})


def test_validate_llm_output_combines_schema_and_pydantic() -> None:
    class Character(BaseModel):
        name: str
        age: int

    schema = {
        "type": "object",
        "required": ["name", "age"],
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    outcome = validate_llm_output(
        {"name": "Alice", "age": 30},
        jsonschema_definition=schema,
        pydantic_model=Character,
    )
    assert outcome.ok


def test_validate_llm_output_pydantic_failure() -> None:
    class Character(BaseModel):
        name: str
        age: int

    outcome = validate_llm_output(
        {"name": "Alice", "age": "thirty"},
        pydantic_model=Character,
    )
    assert not outcome.ok
    assert outcome.errors


def test_validate_llm_output_requires_argument() -> None:
    with pytest.raises(ValueError):
        validate_llm_output({})


def test_outcome_to_dict() -> None:
    outcome = ValidationOutcome(ok=False, errors=[SchemaError(path="/x", message="bad")])
    payload = outcome.to_dict()
    assert payload["ok"] is False
    assert payload["errors"][0]["path"] == "/x"
