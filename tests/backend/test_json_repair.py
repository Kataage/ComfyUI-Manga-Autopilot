"""Tests for the JSON repair loop (spec section 23.4)."""

from __future__ import annotations

import json

import pytest

from manga_autopilot.services.llm_provider import (
    JSONRepairLoop,
    LLMProvider,
    LLMSettings,
    _build_repair_prompt,
    enforce_json_schema,
)


class _ScriptedProvider(LLMProvider):
    """Provider that returns a scripted sequence of responses."""

    def __init__(self, script: list[str]) -> None:
        super().__init__(settings=LLMSettings(type="manual"))
        self._script = list(script)
        self.calls: list[str] = []

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        self.calls.append(prompt)
        if not self._script:
            return "{}"
        return self._script.pop(0)


def test_enforce_json_schema_valid() -> None:
    payload = enforce_json_schema(json.dumps({"a": 1, "b": "x"}), {"required": ["a"]})
    assert payload == {"a": 1, "b": "x"}


def test_enforce_json_schema_missing_required() -> None:
    with pytest.raises(ValueError):
        enforce_json_schema(json.dumps({"a": 1}), {"required": ["a", "b"]})


def test_repair_prompt_uses_spec_text() -> None:
    prompt = _build_repair_prompt("{bad}", {"required": ["a"]}, "missing key")
    assert "以下のJSONはパースに失敗しました" in prompt
    assert "指定Schemaに合うように修復" in prompt
    assert "説明文は不要です" in prompt
    assert "{bad}" in prompt


async def test_repair_loop_succeeds_on_first_try() -> None:
    provider = _ScriptedProvider([json.dumps({"a": 1, "b": "ok"})])
    loop = JSONRepairLoop(max_repair_attempts=2)
    out = await loop.run(provider, "make json", {"required": ["a", "b"]})
    assert out.ok
    assert out.attempts == 1
    assert out.data == {"a": 1, "b": "ok"}


async def test_repair_loop_repairs_broken_json() -> None:
    bad = '{"a": 1,'  # truncated
    good = json.dumps({"a": 1, "b": "x"})
    provider = _ScriptedProvider([bad, good])
    loop = JSONRepairLoop(max_repair_attempts=1)
    out = await loop.run(provider, "make json", {"required": ["a", "b"]})
    assert out.ok
    assert out.attempts == 2
    assert out.data == {"a": 1, "b": "x"}


async def test_repair_loop_repairs_missing_keys() -> None:
    good = json.dumps({"a": 1, "b": "filled"})
    provider = _ScriptedProvider([json.dumps({"a": 1}), good])
    loop = JSONRepairLoop(max_repair_attempts=1)
    out = await loop.run(provider, "make json", {"required": ["a", "b"]})
    assert out.ok
    assert out.data == {"a": 1, "b": "filled"}


async def test_repair_loop_fails_after_max_attempts() -> None:
    provider = _ScriptedProvider(["{bad", "{still bad", "{nope"])
    loop = JSONRepairLoop(max_repair_attempts=2)
    out = await loop.run(provider, "make json", {"required": ["a"]})
    assert not out.ok
    assert out.attempts == 3  # 1 initial + 2 repairs
    assert "could not extract JSON" in (out.error or "")


async def test_repair_loop_zero_attempts_fails() -> None:
    provider = _ScriptedProvider(["{bad"])
    loop = JSONRepairLoop(max_repair_attempts=0)
    out = await loop.run(provider, "make json", {"required": ["a"]})
    assert not out.ok
    assert out.attempts == 1


async def test_provider_complete_json_uses_loop() -> None:
    good = json.dumps({"a": 1, "b": "x"})
    provider = _ScriptedProvider(['{"a": 1}', good])
    data = await provider.complete_json(
        "prompt",
        {"required": ["a", "b"]},
        max_repair_attempts=1,
    )
    assert data == {"a": 1, "b": "x"}


async def test_provider_complete_json_raises_when_exhausted() -> None:
    provider = _ScriptedProvider(["{bad", "{still bad", "{nope"])
    with pytest.raises(ValueError):
        await provider.complete_json(
            "prompt",
            {"required": ["a"]},
            max_repair_attempts=1,
        )
