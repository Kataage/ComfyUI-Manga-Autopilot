"""Character Planner (spec section 13)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from manga_autopilot.services.json_schema_validator import validate_llm_output
from manga_autopilot.services.llm_provider import LLMProvider

log = logging.getLogger(__name__)


CHARACTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "name", "role"],
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "role": {
            "type": "string",
            "enum": ["protagonist", "antagonist", "supporting", "background"],
        },
        "age": {"type": "string"},
        "appearance": {"type": "string"},
        "personality": {"type": "string"},
        "speech_style": {"type": "string"},
        "visual_traits": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}


CHARACTER_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["characters"],
    "properties": {
        "characters": {
            "type": "array",
            "items": CHARACTER_SCHEMA,
        }
    },
}


PROMPT_TEMPLATE = """あなたは漫画のキャラクターデザイナーです。
以下の企画と構成から、登場するキャラクターを提案してください。

条件:
- 出力はJSONのみ
- 各キャラクターに id, name, role を含める
- role は "protagonist" / "antagonist" / "supporting" / "background" のいずれか
- visual_traits には髪型・髪色・目の色など画像生成に役立つ視覚的特徴を 3 個以上含める

企画:
{idea}

ストーリー構成:
{plan}
"""


class CharacterSpec(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=32)
    age: str = ""
    appearance: str = ""
    personality: str = ""
    speech_style: str = ""
    visual_traits: list[str] = Field(default_factory=list)


class CharacterList(BaseModel):
    characters: list[CharacterSpec]


@dataclass
class CharacterPlanner:
    provider: LLMProvider
    max_repair_attempts: int = 1
    system_prompt: str = (
        "You are a manga character designer. Always respond with strict JSON only."
    )

    def build_prompt(self, idea: str, plan: Mapping[str, Any] | str) -> str:
        if isinstance(plan, Mapping):
            plan = json.dumps(plan, ensure_ascii=False, indent=2)
        return PROMPT_TEMPLATE.format(idea=idea, plan=plan)

    async def plan(self, idea: str, plan: Mapping[str, Any] | str) -> CharacterList:
        prompt = self.build_prompt(idea, plan)
        data = await self.provider.complete_json(
            prompt,
            schema=CHARACTER_LIST_SCHEMA,
            system=self.system_prompt,
            max_repair_attempts=self.max_repair_attempts,
        )
        outcome = validate_llm_output(data, jsonschema_definition=CHARACTER_LIST_SCHEMA)
        if not outcome.ok:
            raise ValueError(f"CharacterList validation failed: {outcome.errors}")
        return CharacterList.model_validate(data)


__all__ = [
    "CharacterPlanner",
    "CharacterSpec",
    "CharacterList",
    "CHARACTER_LIST_SCHEMA",
    "CHARACTER_SCHEMA",
    "PROMPT_TEMPLATE",
]
