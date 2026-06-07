"""Tests for the prompt builder (spec section 16)."""

from __future__ import annotations

import json

import pytest

from manga_autopilot.models.page import PanelPlan
from manga_autopilot.services.prompt_builder import (
    GLOBAL_NEGATIVE,
    PromptBuilder,
    PromptSpec,
)


class _StubProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        return self._responses.pop(0) if self._responses else "{}"

    async def complete_json(self, prompt, *, schema, system=None, max_repair_attempts=1):
        from manga_autopilot.services.llm_provider import enforce_json_schema

        text = await self.complete(prompt, schema=schema, system=system)
        try:
            return enforce_json_schema(text, schema)
        except ValueError:
            if max_repair_attempts <= 0:
                raise
            text2 = await self.complete("repair", schema=schema, system=system)
            return enforce_json_schema(text2, schema)


def test_prompt_spec_negative_full() -> None:
    spec = PromptSpec(positive="x", negative="y")
    full = spec.negative_full()
    assert GLOBAL_NEGATIVE in full
    assert "y" in full


def test_prompt_builder_builds_prompt() -> None:
    builder = PromptBuilder(provider=_StubProvider([]))
    out = builder.build_prompt({"panelNumber": 1}, characters=[{"id": "alice"}])
    assert "panelNumber" in out
    assert '"id": "alice"' in out


def test_prompt_builder_accepts_string_characters() -> None:
    builder = PromptBuilder(provider=_StubProvider([]))
    out = builder.build_prompt({"panelNumber": 1}, characters="none")
    assert "none" in out


def test_prompt_builder_accepts_panel_plan_object() -> None:
    builder = PromptBuilder(provider=_StubProvider([]))
    panel = PanelPlan(panel_number=1, purpose="p", shot="s", action="a", emotion="e")
    out = builder.build_prompt(panel)
    assert "panel_number" in out


async def test_prompt_builder_returns_spec() -> None:
    payload = {
        "positive": "1girl, masterpiece",
        "negative": "lowres",
        "characterPrompt": "blue hair",
        "backgroundPrompt": "classroom",
        "actionPrompt": "talking",
        "cameraPrompt": "close-up",
        "emotionPrompt": "calm",
    }
    stub = _StubProvider([json.dumps(payload)])
    builder = PromptBuilder(provider=stub)
    spec = await builder.build({"panelNumber": 1})
    assert isinstance(spec, PromptSpec)
    assert spec.positive == "1girl, masterpiece"
    assert spec.character_prompt == "blue hair"


async def test_prompt_builder_generates_seed_when_missing() -> None:
    payload = {"positive": "x", "negative": "y"}
    stub = _StubProvider([json.dumps(payload)])
    builder = PromptBuilder(provider=stub)
    spec = await builder.build({})
    assert spec.seed != 0


async def test_prompt_builder_rejects_missing_required() -> None:
    bad = json.dumps({"positive": "x"})  # missing negative
    stub = _StubProvider([bad, bad])
    builder = PromptBuilder(provider=stub)
    with pytest.raises((ValueError, KeyError)):
        await builder.build({})
