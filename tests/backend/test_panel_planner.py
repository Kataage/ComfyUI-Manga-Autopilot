"""Tests for the panel planner."""

from __future__ import annotations

import json

import pytest

from manga_autopilot.models.page import PagePlan
from manga_autopilot.services.panel_planner import (
    PANEL_PLAN_SCHEMA,
    PanelPlanList,
    PanelPlanner,
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


async def test_planner_parses_valid_payload() -> None:
    payload = {
        "panels": [
            {
                "panelNumber": 1,
                "purpose": "establishing",
                "shot": "wide",
                "cameraAngle": "eye level",
                "characters": ["alice"],
                "background": "classroom",
                "action": "talking",
                "emotion": "calm",
                "visualPriority": "character",
                "dialogue": [
                    {
                        "characterId": "alice",
                        "speaker": "Alice",
                        "text": "Hello.",
                        "type": "speech",
                        "balloonPosition": "top",
                    }
                ],
                "sfx": [{"text": "ding", "intensity": 1, "position": "top"}],
            },
            {
                "panelNumber": 2,
                "purpose": "reaction",
                "shot": "close",
                "action": "smile",
                "emotion": "warm",
            },
        ]
    }
    stub = _StubProvider([json.dumps(payload)])
    planner = PanelPlanner(provider=stub, panel_count=2)
    result = await planner.plan({"pageNumber": 1, "panelCount": 2})
    assert isinstance(result, PanelPlanList)
    assert len(result.panels) == 2
    assert result.panels[0].dialogue[0].text == "Hello."
    assert result.panels[0].sfx[0].text == "ding"
    assert result.panels[1].shot == "close"


async def test_planner_accepts_page_plan_object() -> None:
    payload = {
        "panels": [
            {
                "panelNumber": 1,
                "purpose": "p",
                "shot": "s",
                "action": "a",
                "emotion": "e",
            }
        ]
    }
    stub = _StubProvider([json.dumps(payload)])
    planner = PanelPlanner(provider=stub, panel_count=1)
    page = PagePlan(page_number=1, panel_count=1, summary="x")
    result = await planner.plan(page)
    assert len(result.panels) == 1


async def test_planner_rejects_missing_required() -> None:
    bad = json.dumps({"panels": [{"panelNumber": 1}]})  # missing purpose etc
    stub = _StubProvider([bad, bad])
    planner = PanelPlanner(provider=stub, panel_count=1)
    with pytest.raises((ValueError, KeyError)):
        await planner.plan({})


def test_panel_plan_schema_visual_priority_enum() -> None:
    enum = PANEL_PLAN_SCHEMA["properties"]["panels"]["items"]["properties"][
        "visualPriority"
    ]["enum"]
    assert set(enum) == {"character", "action", "background", "emotion"}


def test_panel_plan_list_to_dict() -> None:
    payload = {
        "panels": [
            {
                "panelNumber": 1,
                "purpose": "p",
                "shot": "s",
                "action": "a",
                "emotion": "e",
            }
        ]
    }
    stub = _StubProvider([json.dumps(payload)])

    async def _run() -> None:
        result = await PanelPlanner(provider=stub, panel_count=1).plan({})
        data = result.to_dict()
        assert data[0]["panel_number"] == 1

    import asyncio

    asyncio.run(_run())
