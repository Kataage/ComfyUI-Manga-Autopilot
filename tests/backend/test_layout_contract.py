from __future__ import annotations

import json

from manga_autopilot.services.llm_provider import LLMProvider, LLMSettings
from manga_autopilot.services.page_planner import PagePlanner
from manga_autopilot.services.page_templates import (
    fallback_grid,
    layout_catalog,
)
from manga_autopilot.services.semantic_validation import validate_page_layouts


class _SequenceProvider(LLMProvider):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__(LLMSettings(type="manual"))
        self.responses = [json.dumps(response) for response in responses]

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        return self.responses.pop(0)


def test_layout_catalog_exposes_ids_counts_and_japanese_reading_order() -> None:
    catalog = {item["layout_id"]: item for item in layout_catalog("page")}

    assert catalog["page_4_grid"] == {
        "layout_id": "page_4_grid",
        "panel_count": 4,
        "reading_order": [2, 1, 4, 3],
    }


def test_fallback_grid_is_deterministic_and_matches_panel_count() -> None:
    first = fallback_grid(7)
    second = fallback_grid(7)

    assert first == second
    assert first.template_id == "fallback_grid_7"
    assert len(first.panels) == 7


def test_page_layout_validation_rejects_unknown_and_slot_mismatch() -> None:
    issues = validate_page_layouts(
        [
            {"pageNumber": 1, "panelCount": 3, "layoutId": "page_2_horizontal"},
            {"pageNumber": 2, "panelCount": 1, "layoutId": "invented"},
        ],
        {"page_2_horizontal": 2},
    )

    assert {(issue.path, issue.code) for issue in issues} == {
        ("/pages/0/panelCount", "layout_slot_mismatch"),
        ("/pages/1/layoutId", "unknown_layout"),
    }


def test_missing_layout_uses_named_fallback_with_warning() -> None:
    issues = validate_page_layouts(
        [{"pageNumber": 1, "panelCount": 3}],
        {"page_3_t": 3},
    )

    assert [(issue.code, issue.severity, issue.fallback) for issue in issues] == [
        ("layout_fallback", "warning", "fallback_grid_3"),
    ]


async def test_strict_page_planner_repairs_layout_slot_mismatch() -> None:
    provider = _SequenceProvider(
        [
            {
                "pages": [
                    {
                        "pageNumber": 1,
                        "summary": "intro",
                        "panelCount": 3,
                        "layoutId": "page_2_horizontal",
                    }
                ]
            },
            {
                "pages": [
                    {
                        "pageNumber": 1,
                        "summary": "intro",
                        "panelCount": 2,
                        "layoutId": "page_2_horizontal",
                    }
                ]
            },
        ]
    )

    result = await PagePlanner(
        provider=provider,
        page_count=1,
        strict=True,
        layout_slots={"page_2_horizontal": 2},
    ).plan({"title": "x"})

    assert result.pages[0].layout_id == "page_2_horizontal"
    assert result.pages[0].panel_count == 2
    assert provider.responses == []
