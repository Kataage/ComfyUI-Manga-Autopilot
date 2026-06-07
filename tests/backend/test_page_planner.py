"""Tests for the page planner."""

from __future__ import annotations

import json

import pytest

from manga_autopilot.services.page_planner import (
    PAGE_PLAN_SCHEMA,
    PROMPT_TEMPLATE,
    PagePlanList,
    PagePlanner,
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


def test_prompt_template_substitutes() -> None:
    out = PROMPT_TEMPLATE.format(page_count=5, plan='{"title": "X"}')
    assert "ページ数は 5 ページ" in out
    assert '"title": "X"' in out


def test_page_plan_schema_requires_pages() -> None:
    assert PAGE_PLAN_SCHEMA["required"] == ["pages"]


async def test_planner_parses_valid_payload() -> None:
    payload = {
        "pages": [
            {
                "pageNumber": 1,
                "summary": "intro",
                "emotionalGoal": "calm",
                "visualGoal": "classroom",
                "panelCount": 3,
            },
            {
                "pageNumber": 2,
                "summary": "twist",
                "emotionalGoal": "shock",
                "visualGoal": "close-up",
                "panelCount": 2,
                "cliffhanger": "the letter",
            },
        ]
    }
    stub = _StubProvider([json.dumps(payload)])
    planner = PagePlanner(provider=stub, page_count=2)
    result = await planner.plan({"title": "X"})
    assert isinstance(result, PagePlanList)
    assert len(result.pages) == 2
    assert result.pages[0].panel_count == 3
    assert result.pages[1].cliffhanger == "the letter"


async def test_planner_handles_string_plan() -> None:
    payload = {
        "pages": [
            {"pageNumber": 1, "summary": "intro", "panelCount": 1}
        ]
    }
    stub = _StubProvider([json.dumps(payload)])
    planner = PagePlanner(provider=stub, page_count=1)
    result = await planner.plan("a string plan")
    assert len(result.pages) == 1


async def test_planner_rejects_missing_required() -> None:
    bad = json.dumps({"pages": [{"pageNumber": 1}]})  # missing summary/panelCount
    stub = _StubProvider([bad, bad])
    planner = PagePlanner(provider=stub, page_count=1)
    with pytest.raises((ValueError, Exception)):
        await planner.plan({})


def test_planner_validates_panel_count_range_via_schema() -> None:
    schema = PAGE_PLAN_SCHEMA
    items = schema["properties"]["pages"]["items"]
    panel_count = items["properties"]["panelCount"]
    assert panel_count["minimum"] == 1
    assert panel_count["maximum"] == 24
