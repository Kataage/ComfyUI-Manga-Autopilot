"""Page Planner (spec section 14.4)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from manga_autopilot.models.page import PagePlan
from manga_autopilot.services.json_schema_validator import validate_llm_output
from manga_autopilot.services.llm_provider import LLMProvider
from manga_autopilot.services.semantic_validation import (
    validate_page_layouts,
    validate_page_sequence,
)

log = logging.getLogger(__name__)


PAGE_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["pages"],
    "properties": {
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["pageNumber", "summary", "panelCount"],
                "properties": {
                    "pageNumber": {"type": "integer", "minimum": 1},
                    "summary": {"type": "string"},
                    "emotionalGoal": {"type": "string"},
                    "visualGoal": {"type": "string"},
                    "panelCount": {"type": "integer", "minimum": 1, "maximum": 24},
                    "layoutId": {"type": "string"},
                    "cliffhanger": {"type": "string"},
                },
            },
        }
    },
}


PROMPT_TEMPLATE = """あなたは漫画の編集者です。
以下のストーリー構成を、各ページ単位の PagePlan リストに展開してください。

条件:
- 出力はJSONのみ
- ページ数は {page_count} ページ
- 各ページに pageNumber, summary, emotionalGoal, visualGoal, panelCount を含める
- panelCount は 1〜8 の範囲で指定
- cliffhanger は必要なら含める

ストーリー:
{plan}
"""


class PagePlanList(BaseModel):
    pages: list[PagePlan] = Field(default_factory=list)


@dataclass
class PagePlanner:
    provider: LLMProvider
    page_count: int = 4
    max_repair_attempts: int = 1
    strict: bool = False
    layout_slots: dict[str, int] = field(default_factory=dict)
    system_prompt: str = (
        "You are a manga editor. Always respond with strict JSON only."
    )

    def build_prompt(self, plan: Mapping[str, Any] | str) -> str:
        if isinstance(plan, Mapping):
            plan = json.dumps(plan, ensure_ascii=False, indent=2)
        return PROMPT_TEMPLATE.format(page_count=self.page_count, plan=plan)

    async def plan(self, story_plan: Mapping[str, Any] | str) -> PagePlanList:
        prompt = self.build_prompt(story_plan)
        validation_options: dict[str, Any] = {}
        if self.strict:
            def _validate_semantics(data: dict[str, Any]) -> list[str]:
                issues = validate_page_sequence(
                    data.get("pages", []),
                    self.page_count,
                )
                if self.layout_slots:
                    issues.extend(
                        issue
                        for issue in validate_page_layouts(
                            data.get("pages", []),
                            self.layout_slots,
                        )
                        if issue.severity == "error"
                    )
                return [
                    f"{issue.path}: {issue.message}"
                    for issue in issues
                ]

            validation_options["semantic_validator"] = _validate_semantics
        data = await self.provider.complete_json(
            prompt,
            schema=PAGE_PLAN_SCHEMA,
            system=self.system_prompt,
            max_repair_attempts=self.max_repair_attempts,
            **validation_options,
        )
        outcome = validate_llm_output(data, jsonschema_definition=PAGE_PLAN_SCHEMA)
        if not outcome.ok:
            raise ValueError(f"PagePlanList validation failed: {outcome.errors}")
        return PagePlanList.model_validate(self._to_pydantic_payload(data))

    @staticmethod
    def _to_pydantic_payload(data: Mapping[str, Any]) -> dict[str, Any]:
        """Translate camelCase LLM keys to snake_case for :class:`PagePlan`."""

        translated_pages: list[dict[str, Any]] = []
        for page in data.get("pages", []):
            translated_pages.append(
                {
                    "page_number": int(page["pageNumber"]),
                    "summary": page.get("summary", ""),
                    "emotional_goal": page.get("emotionalGoal", ""),
                    "visual_goal": page.get("visualGoal", ""),
                    "panel_count": int(page.get("panelCount", 1)),
                    "layout_id": page.get("layoutId"),
                    "cliffhanger": page.get("cliffhanger"),
                }
            )
        return {"pages": translated_pages}


__all__ = [
    "PagePlanner",
    "PagePlanList",
    "PAGE_PLAN_SCHEMA",
    "PROMPT_TEMPLATE",
]
