"""Tests for the story planner."""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web

from manga_autopilot.models.story import StoryPlan
from manga_autopilot.services.llm_provider import (
    LLMSettings,
    OpenAICompatibleProvider,
)
from manga_autopilot.services.story_planner import (
    PROMPT_TEMPLATE,
    STORY_PLAN_SCHEMA,
    StoryPlanner,
    dump_story_plan,
)


class _StubProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
    ) -> str:
        self.calls.append({"prompt": prompt, "schema": schema, "system": system})
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


async def test_planner_builds_prompt() -> None:
    stub = _StubProvider([])
    planner = StoryPlanner(provider=stub, page_count=6, language="ja", genre="fantasy")
    prompt = planner.build_prompt("A cat becomes a hero.")
    assert "ページ数: 6" in prompt
    assert "A cat becomes a hero." in prompt
    assert "言語: ja" in prompt


async def test_planner_parses_valid_payload() -> None:
    payload = {
        "title": "Cat Hero",
        "logline": "A cat saves the village.",
        "theme": "courage",
        "genre": "fantasy",
        "mood": "warm",
        "acts": [
            {
                "id": "act-1",
                "name": "Setup",
                "startPage": 1,
                "endPage": 4,
                "summary": "intro",
                "emotionalArc": "rising",
            }
        ],
        "pages": [
            {
                "pageNumber": 1,
                "summary": "Meet the cat",
                "emotionalGoal": "curiosity",
                "visualGoal": "village",
                "panelCount": 2,
            },
            {
                "pageNumber": 2,
                "summary": "Call to action",
                "emotionalGoal": "tension",
                "visualGoal": "storm",
                "panelCount": 3,
            },
        ],
    }
    stub = _StubProvider([json.dumps(payload)])
    planner = StoryPlanner(provider=stub)
    plan = await planner.plan("An idea")
    assert isinstance(plan, StoryPlan)
    assert plan.title == "Cat Hero"
    assert len(plan.pages) == 2
    assert plan.pages[0].page_number == 1


async def test_planner_repairs_invalid_json() -> None:
    bad = "Here is your plan: not valid json"
    good = json.dumps(
        {
            "title": "Cat Hero",
            "pages": [{"pageNumber": 1, "summary": "x", "panelCount": 1}],
        }
    )
    stub = _StubProvider([bad, good])
    planner = StoryPlanner(provider=stub, max_repair_attempts=1)
    plan = await planner.plan("An idea")
    assert plan.title == "Cat Hero"
    # The first call should include the schema; both calls record schema.
    assert len(stub.calls) == 2


async def test_planner_rejects_payload_missing_required() -> None:
    bad = json.dumps({"title": "Only title"})
    stub = _StubProvider([bad, bad])
    planner = StoryPlanner(provider=stub, max_repair_attempts=1)
    with pytest.raises(ValueError):
        await planner.plan("An idea")


async def test_dump_story_plan_round_trip() -> None:
    payload = {
        "title": "Cat Hero",
        "logline": "A cat saves the village.",
        "pages": [
            {
                "pageNumber": 1,
                "summary": "Meet the cat",
                "emotionalGoal": "curiosity",
                "visualGoal": "village",
                "panelCount": 2,
            }
        ],
    }
    stub = _StubProvider([json.dumps(payload)])
    planner = StoryPlanner(provider=stub)
    plan = await planner.plan("An idea")
    dumped = dump_story_plan(plan)
    reparsed = json.loads(dumped)
    assert reparsed["title"] == "Cat Hero"


def test_prompt_template_substitutes_variables() -> None:
    out = PROMPT_TEMPLATE.format(page_count=3, language="en", genre="sci-fi", idea="X")
    assert "ページ数: 3" in out
    assert "言語: en" in out
    assert "ジャンル: sci-fi" in out


def test_story_plan_schema_contains_required_keys() -> None:
    for key in ("title", "pages"):
        assert key in STORY_PLAN_SCHEMA["required"]


@pytest.fixture()
async def openai_server(aiohttp_server):
    async def handle_chat(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Stub",
                                    "pages": [
                                        {"pageNumber": 1, "summary": "x", "panelCount": 1}
                                    ],
                                }
                            )
                        }
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_chat)
    return await aiohttp_server(app)


async def test_planner_with_real_openai_compatible_provider(openai_server) -> None:
    settings = LLMSettings(
        type="openai_compatible",
        endpoint=f"http://{openai_server.host}:{openai_server.port}",
        model="gpt",
    )
    planner = StoryPlanner(provider=OpenAICompatibleProvider(settings))
    plan = await planner.plan("An idea")
    assert plan.title == "Stub"
