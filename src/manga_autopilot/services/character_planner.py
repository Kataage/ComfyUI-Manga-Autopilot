"""Character Planner (spec section 13)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from manga_autopilot.models.character import (
    Character,
    CharacterAppearance,
    ColorPalette,
)
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


#: Roles the persisted :class:`Character` model accepts.
_KNOWN_ROLES = {"protagonist", "heroine", "villain", "support", "mob"}

#: What a trait has to say for us to read a colour out of it.
_HAIR_MARKERS = ("hair",)
_EYE_MARKERS = ("eye", "eyes")

#: Used when the planner did not describe a required appearance field. Saying
#: "unspecified" is honest; inventing "black hair" would put a fact into the
#: character card that nobody chose.
UNSPECIFIED = "unspecified"


def _trait_value(traits: Sequence[str], markers: Sequence[str]) -> str:
    """Return the first trait mentioning any marker, with the marker removed.

    ``["blue hair"]`` yields ``"blue"``. A trait that is only the marker itself
    (``"hair"``) carries no information and is skipped.
    """
    for trait in traits:
        lowered = trait.lower()
        for marker in markers:
            if marker not in lowered.split() and marker not in lowered:
                continue
            words = [word for word in trait.split() if word.lower() not in markers]
            if words:
                return " ".join(words)
    return ""


def spec_to_character(spec: CharacterSpec) -> Character:
    """Convert a planner :class:`CharacterSpec` into a persistable ``Character``.

    The planner produces free text and a trait list; the persisted card needs a
    structured appearance. Hair and eye colour are read out of the traits when
    they say so, and every trait is kept verbatim under
    ``appearance.distinctive_features`` so nothing the planner said is lost.

    An unrecognised role falls back to ``support`` rather than failing: the role
    vocabulary is ours, not the planner's.
    """
    traits = list(spec.visual_traits)
    role = spec.role.strip().lower()
    return Character(
        id=spec.id,
        name=spec.name,
        role=role if role in _KNOWN_ROLES else "support",
        description=spec.appearance,
        personality=spec.personality,
        age_appearance=spec.age[:64],
        appearance=CharacterAppearance(
            hair_color=_trait_value(traits, _HAIR_MARKERS) or UNSPECIFIED,
            hair_style=_trait_value(traits, _HAIR_MARKERS) or UNSPECIFIED,
            eye_color=_trait_value(traits, _EYE_MARKERS) or UNSPECIFIED,
            distinctive_features=traits[:32],
        ),
        color_palette=ColorPalette(primary="#000000"),
        consistency_prompt=", ".join(traits)[:1024],
    )


__all__ = [
    "UNSPECIFIED",
    "CharacterPlanner",
    "CharacterSpec",
    "CharacterList",
    "CHARACTER_LIST_SCHEMA",
    "CHARACTER_SCHEMA",
    "PROMPT_TEMPLATE",
    "spec_to_character",
]
