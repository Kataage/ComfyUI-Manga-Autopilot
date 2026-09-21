from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from manga_autopilot.routes.autopilot_routes import (
    _make_plan_pages,
    _make_plan_panels,
)
from manga_autopilot.services.autopilot import AutopilotRun, AutopilotStateMachine
from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.services.panel_planner import PanelPlanner


class _UnusedProvider:
    async def complete_json(self, *args, **kwargs):
        raise AssertionError("test only builds the prompt")


class _JsonProvider(LLMProvider):
    def __init__(self, payload: dict) -> None:
        super().__init__(LLMSettings(type="manual"))
        self.payload = payload

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        return json.dumps(self.payload)


class _FailProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(LLMSettings(type="manual"))

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        raise AssertionError("Anima page planning made a second LLM call")


def test_panel_prompt_contains_only_page_context_needed_for_continuity() -> None:
    planner = PanelPlanner(
        provider=_UnusedProvider(),
        panel_count=2,
        story_bible={"rules": ["spirits stay behind the torii"]},
        scene_state={
            "location": "old_shrine",
            "characters": {"hero": {"held_items": ["umbrella"]}},
        },
        active_characters=[
            {
                "id": "hero",
                "appearance": {"hair": "black bob"},
                "speech": {"tone": "formal"},
            }
        ],
        layouts=[
            {
                "layout_id": "page_2_horizontal",
                "panel_count": 2,
                "reading_order": [1, 2],
            }
        ],
    )

    prompt = planner.build_prompt(
        {
            "pageNumber": 2,
            "summary": "hero enters",
            "panelCount": 2,
            "layoutId": "page_2_horizontal",
        }
    )

    assert "spirits stay behind the torii" in prompt
    assert "old_shrine" in prompt
    assert "umbrella" in prompt
    assert "black bob" in prompt
    assert "formal" in prompt
    assert "page_2_horizontal" in prompt
    assert "hero enters" in prompt


async def test_panel_planner_parses_scene_delta_events() -> None:
    provider = _JsonProvider(
        {
            "panels": [
                {
                    "panelNumber": 1,
                    "purpose": "arrival",
                    "shot": "wide",
                    "action": "hero arrives",
                    "emotion": "nervous",
                    "characters": ["hero"],
                    "sceneDelta": {
                        "events": [
                            {
                                "kind": "acquire_object",
                                "character_id": "hero",
                                "value": "umbrella",
                            }
                        ]
                    },
                }
            ]
        }
    )

    result = await PanelPlanner(
        provider=provider,
        panel_count=1,
        strict=True,
        character_ids={"hero"},
    ).plan({"pageNumber": 1, "panelCount": 1})

    event = result.panels[0].scene_delta.events[0]
    assert (event.kind, event.character_id, event.value) == (
        "acquire_object",
        "hero",
        "umbrella",
    )


async def test_anima_page_planning_reuses_global_story_pages(tmp_path: Path) -> None:
    app = web.Application()
    app["manga_llm_provider"] = _FailProvider()
    run = AutopilotRun(
        project_id="proj",
        machine=AutopilotStateMachine(project_id="proj"),
        input={"generation_profile_id": "anima_turbo", "page_count": 1},
    )
    run.artefacts["plan_story"] = {
        "title": "x",
        "story_bible": {"rules": ["keep the umbrella"]},
        "pages": [
            {
                "page_number": 1,
                "summary": "arrival",
                "panel_count": 1,
                "layout_id": "fallback_grid_1",
            }
        ],
    }

    result = await _make_plan_pages(app, "proj", tmp_path)(run)

    assert result[0]["summary"] == "arrival"
    assert result[0]["layout_id"] == "fallback_grid_1"


async def test_anima_panel_planning_persists_reduced_scene_state(
    tmp_path: Path,
) -> None:
    provider = _JsonProvider(
        {
            "panels": [
                {
                    "panelNumber": 1,
                    "purpose": "arrival",
                    "shot": "wide",
                    "action": "rain falls",
                    "emotion": "quiet",
                    "sceneDelta": {
                        "events": [
                            {"kind": "set_location", "value": "old_shrine"},
                            {"kind": "set_time", "value": "evening"},
                        ]
                    },
                }
            ]
        }
    )
    app = web.Application()
    app["manga_llm_provider"] = provider
    run = AutopilotRun(
        project_id="proj",
        machine=AutopilotStateMachine(project_id="proj"),
        input={
            "generation_profile_id": "anima_turbo",
            "panels_per_page": 1,
        },
    )
    run.artefacts["plan_story"] = {
        "story_bible": {"rules": ["rain continues"]},
    }
    run.artefacts["plan_pages"] = [
        {
            "page_number": 1,
            "summary": "arrival",
            "panel_count": 1,
            "layout_id": "fallback_grid_1",
        }
    ]

    records = await _make_plan_panels(app, "proj", tmp_path)(run)
    state = json.loads(
        (tmp_path / "projects" / "proj" / "scene_state.json").read_text(
            encoding="utf-8"
        )
    )

    assert len(records) == 1
    assert state["location"] == "old_shrine"
    assert state["time"] == "evening"
