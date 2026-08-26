from __future__ import annotations

import json

from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.services.page_planner import PagePlanner
from manga_autopilot.services.panel_planner import PanelPlanner
from manga_autopilot.services.semantic_validation import (
    validate_page_sequence,
    validate_panel_sequence,
)
from manga_autopilot.services.story_planner import StoryPlanner


class _SequenceProvider(LLMProvider):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__(LLMSettings(type="manual"))
        self.responses = [json.dumps(response) for response in responses]

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        return self.responses.pop(0)


def test_page_sequence_rejects_wrong_count() -> None:
    issues = validate_page_sequence(
        [{"pageNumber": 1}, {"pageNumber": 2}],
        expected_count=4,
    )

    assert [(issue.path, issue.code) for issue in issues] == [
        ("/pages", "page_count"),
    ]


def test_page_sequence_rejects_duplicate_and_missing_numbers() -> None:
    issues = validate_page_sequence(
        [{"pageNumber": 1}, {"pageNumber": 1}],
        expected_count=2,
    )

    assert [(issue.path, issue.code) for issue in issues] == [
        ("/pages", "page_number_sequence"),
    ]


def test_panel_sequence_rejects_unknown_character_and_layout() -> None:
    issues = validate_panel_sequence(
        [
            {
                "panelNumber": 1,
                "characters": ["hero", "stranger"],
            }
        ],
        expected_count=1,
        character_ids={"hero"},
        layout_id="invented_layout",
        registered_layout_ids={"page_2_horizontal"},
    )

    assert {(issue.path, issue.code) for issue in issues} == {
        ("/layoutId", "unknown_layout"),
        ("/panels/0/characters/1", "unknown_character"),
    }


def test_panel_sequence_accepts_valid_references() -> None:
    issues = validate_panel_sequence(
        [
            {"panelNumber": 1, "characters": ["hero"]},
            {"panelNumber": 2, "characters": []},
        ],
        expected_count=2,
        character_ids={"hero"},
        layout_id="page_2_horizontal",
        registered_layout_ids={"page_2_horizontal"},
    )

    assert issues == []


async def test_strict_page_planner_repairs_semantic_count_failure() -> None:
    provider = _SequenceProvider(
        [
            {
                "pages": [
                    {"pageNumber": 1, "summary": "one", "panelCount": 1},
                ]
            },
            {
                "pages": [
                    {"pageNumber": 1, "summary": "one", "panelCount": 1},
                    {"pageNumber": 2, "summary": "two", "panelCount": 1},
                ]
            },
        ]
    )

    result = await PagePlanner(
        provider=provider,
        page_count=2,
        strict=True,
    ).plan({"title": "x"})

    assert [page.page_number for page in result.pages] == [1, 2]
    assert provider.responses == []


async def test_strict_panel_planner_repairs_unknown_character() -> None:
    provider = _SequenceProvider(
        [
            {
                "panels": [
                    {
                        "panelNumber": 1,
                        "purpose": "intro",
                        "shot": "wide",
                        "action": "arrives",
                        "emotion": "calm",
                        "characters": ["stranger"],
                    }
                ]
            },
            {
                "panels": [
                    {
                        "panelNumber": 1,
                        "purpose": "intro",
                        "shot": "wide",
                        "action": "arrives",
                        "emotion": "calm",
                        "characters": ["hero"],
                    }
                ]
            },
        ]
    )

    result = await PanelPlanner(
        provider=provider,
        panel_count=1,
        strict=True,
        character_ids={"hero"},
    ).plan({"pageNumber": 1, "panelCount": 1})

    assert result.panels[0].characters == ["hero"]
    assert provider.responses == []


async def test_strict_story_planner_repairs_wrong_global_page_count() -> None:
    provider = _SequenceProvider(
        [
            {
                "title": "x",
                "pages": [
                    {"pageNumber": 1, "summary": "one", "panelCount": 1},
                ],
            },
            {
                "title": "x",
                "pages": [
                    {"pageNumber": 1, "summary": "one", "panelCount": 1},
                    {"pageNumber": 2, "summary": "two", "panelCount": 1},
                ],
            },
        ]
    )

    result = await StoryPlanner(
        provider=provider,
        page_count=2,
        strict=True,
    ).plan("idea")

    assert [page.page_number for page in result.pages] == [1, 2]
    assert provider.responses == []
