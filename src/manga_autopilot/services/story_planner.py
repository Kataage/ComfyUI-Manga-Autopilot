"""Story Planner — LLM-driven generator of a StoryPlan (spec section 14)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from manga_autopilot.models.story import Act, StoryPlan
from manga_autopilot.services.json_schema_validator import validate_llm_output
from manga_autopilot.services.llm_provider import LLMProvider
from manga_autopilot.services.semantic_validation import validate_page_sequence

log = logging.getLogger(__name__)


STORY_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "pages"],
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "theme": {"type": "string"},
        "genre": {"type": "string"},
        "mood": {"type": "string"},
        "storyBible": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "genre": {"type": "string"},
                "tone": {"type": "string"},
                "theme": {"type": "string"},
                "world": {"type": "string"},
                "rules": {"type": "array", "items": {"type": "string"}},
                "timeline": {"type": "array", "items": {"type": "string"}},
                "locations": {"type": "object", "additionalProperties": {"type": "string"}},
                "important_objects": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "relationships": {"type": "array", "items": {"type": "string"}},
                "foreshadowing": {"type": "array", "items": {"type": "string"}},
                "resolved_events": {"type": "array", "items": {"type": "string"}},
                "unresolved_events": {"type": "array", "items": {"type": "string"}},
            },
        },
        "acts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "name", "startPage", "endPage"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "startPage": {"type": "integer"},
                    "endPage": {"type": "integer"},
                    "summary": {"type": "string"},
                    "emotionalArc": {"type": "string"},
                },
            },
        },
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["pageNumber", "panelCount", "summary"],
                "properties": {
                    "pageNumber": {"type": "integer"},
                    "summary": {"type": "string"},
                    "emotionalGoal": {"type": "string"},
                    "visualGoal": {"type": "string"},
                    "panelCount": {"type": "integer"},
                    "layoutId": {"type": "string"},
                    "cliffhanger": {"type": "string"},
                },
            },
        },
    },
}


PROMPT_TEMPLATE = """あなたは漫画原作者です。
以下の企画を、指定ページ数の漫画構成にしてください。

条件:
- 出力はJSONのみ
- ページ数: {page_count}
- 言語: {language}
- ジャンル: {genre}
- 1ページごとに summary, emotionalGoal, visualGoal, panelCount を含める
- storyBible に世界、ルール、場所、重要物、関係、伏線、解決済み・未解決イベントを含める
- セリフは短くする
- 各ページの目的が重複しないようにする
- 最終ページには読後感または次への引きを入れる
{layout_rules}
企画:
{idea}
"""

#: Appended when the caller knows which layouts exist. Without it the planner
#: has no way to know the vocabulary and invents ids like "standard_linear",
#: which strict validation then rejects - correctly, but only after a full
#: planning round trip has been paid for.
LAYOUT_RULES_TEMPLATE = """- layoutId は次の登録済みレイアウトからのみ選ぶ。panelCount は選んだレイアウトのコマ数と一致させる:
{layout_lines}
- 上のどれとも合わない場合は layoutId を省略する（自動で等分割グリッドが割り当てられる）
"""


@dataclass
class StoryPlanner:
    provider: LLMProvider
    page_count: int = 4
    language: str = "ja"
    genre: str = "fantasy"
    max_repair_attempts: int = 1
    strict: bool = False
    layouts: list[dict[str, Any]] = field(default_factory=list)
    """Registered page layouts, as ``{"layout_id": ..., "panel_count": ...}``."""
    system_prompt: str = (
        "You are a meticulous manga story planner. Always respond with strict JSON only."
    )

    _issues: list[str] = field(default_factory=list)

    def build_prompt(self, idea: str) -> str:
        return PROMPT_TEMPLATE.format(
            page_count=self.page_count,
            language=self.language,
            genre=self.genre,
            layout_rules=self._layout_rules(),
            idea=idea,
        )

    def _layout_rules(self) -> str:
        """Describe the registered layouts, so the planner picks from them.

        Strict planning rejects an unregistered ``layoutId``. Telling the model
        the vocabulary up front is cheaper than discovering it through a failed
        validation, and much cheaper than a run that plans nothing.
        """
        if not self.layouts:
            return ""
        lines = chr(10).join(
            f"  - {item['layout_id']} ({item['panel_count']}コマ)" for item in self.layouts
        )
        return LAYOUT_RULES_TEMPLATE.format(layout_lines=lines)

    async def plan(self, idea: str) -> StoryPlan:
        prompt = self.build_prompt(idea)
        validation_options: dict[str, Any] = {}
        if self.strict:
            def _validate_semantics(data: dict[str, Any]) -> list[str]:
                messages = [
                    f"{issue.path}: {issue.message}"
                    for issue in validate_page_sequence(
                        data.get("pages", []),
                        self.page_count,
                    )
                ]
                if "storyBible" not in data:
                    messages.append("/storyBible: Story Bible is required in strict mode")
                return messages

            validation_options["semantic_validator"] = _validate_semantics
        data = await self.provider.complete_json(
            prompt,
            schema=STORY_PLAN_SCHEMA,
            system=self.system_prompt,
            max_repair_attempts=self.max_repair_attempts,
            **validation_options,
        )
        return self._to_model(data, idea)

    # ------------------------------------------------------------------ utils
    def _to_model(self, data: Mapping[str, Any], idea: str) -> StoryPlan:
        # Validate once more with the model layer so we get typed errors.
        outcome = validate_llm_output(data, jsonschema_definition=STORY_PLAN_SCHEMA)
        if not outcome.ok:
            self._issues.extend(e.message for e in outcome.errors)
            raise ValueError(f"StoryPlan validation failed: {outcome.errors}")
        acts = [self._act_from(a) for a in data.get("acts", [])]
        pages = self._pages_from(data.get("pages", []), idea)
        return StoryPlan(
            title=data["title"],
            logline=data.get("logline", ""),
            theme=data.get("theme", ""),
            genre=data.get("genre", self.genre),
            mood=data.get("mood", ""),
            story_bible=data.get("storyBible") or {},
            acts=acts,
            pages=pages,
        )

    @staticmethod
    def _act_from(payload: Mapping[str, Any]) -> Act:
        return Act(
            id=payload["id"],
            name=payload["name"],
            start_page=int(payload["startPage"]),
            end_page=int(payload["endPage"]),
            summary=payload.get("summary", ""),
            emotional_arc=payload.get("emotionalArc", ""),
        )

    @staticmethod
    def _pages_from(payloads: list[Mapping[str, Any]], idea: str) -> list:
        from manga_autopilot.models.page import PagePlan

        out = []
        for item in payloads:
            out.append(
                PagePlan(
                    page_number=int(item["pageNumber"]),
                    summary=item.get("summary", ""),
                    emotional_goal=item.get("emotionalGoal", ""),
                    visual_goal=item.get("visualGoal", ""),
                    panel_count=max(1, int(item.get("panelCount", 1))),
                    cliffhanger=item.get("cliffhanger"),
                    layout_id=item.get("layoutId"),
                )
            )
        return out


def dump_story_plan(plan: StoryPlan) -> str:
    return json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)


__all__ = ["StoryPlanner", "STORY_PLAN_SCHEMA", "PROMPT_TEMPLATE", "dump_story_plan"]
