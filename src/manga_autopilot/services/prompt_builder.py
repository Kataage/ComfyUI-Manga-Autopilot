"""Prompt Builder (spec sections 16.1-16.5)."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from manga_autopilot.models.page import PanelPlan
from manga_autopilot.services.json_schema_validator import validate_llm_output
from manga_autopilot.services.llm_provider import LLMProvider

log = logging.getLogger(__name__)


GLOBAL_NEGATIVE = (
    "low quality, worst quality, blurry, bad anatomy, bad hands, extra fingers, "
    "missing fingers, deformed face, text, watermark, logo, cropped, "
    "duplicate character"
)
CHARACTER_NEGATIVE = (
    "different hair color, different eye color, different outfit, wrong weapon, "
    "inconsistent costume, wrong age, different character"
)
MANGA_NEGATIVE = (
    "speech text in image, unreadable letters, random letters, broken panel "
    "border, excessive background clutter"
)


class PromptSpec(BaseModel):
    positive: str
    negative: str
    character_prompt: str = ""
    background_prompt: str = ""
    action_prompt: str = ""
    camera_prompt: str = ""
    emotion_prompt: str = ""
    style_prompt: str = ""
    quality_prompt: str = "masterpiece, best quality, highly detailed"
    seed: int = 0
    width: int = Field(default=1024, ge=64, le=4096)
    height: int = Field(default=1024, ge=64, le=4096)
    steps: int = Field(default=28, ge=1, le=200)
    cfg: float = Field(default=7.0, ge=0.0, le=30.0)
    sampler: str = "euler_ancestral"
    scheduler: str = "normal"

    def negative_full(self) -> str:
        return ", ".join(
            part for part in (GLOBAL_NEGATIVE, CHARACTER_NEGATIVE, MANGA_NEGATIVE, self.negative) if part
        )


PROMPT_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["positive", "negative"],
    "properties": {
        "positive": {"type": "string"},
        "negative": {"type": "string"},
        "characterPrompt": {"type": "string"},
        "backgroundPrompt": {"type": "string"},
        "actionPrompt": {"type": "string"},
        "cameraPrompt": {"type": "string"},
        "emotionPrompt": {"type": "string"},
        "stylePrompt": {"type": "string"},
        "qualityPrompt": {"type": "string"},
        "seed": {"type": "integer"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "steps": {"type": "integer"},
        "cfg": {"type": "number"},
        "sampler": {"type": "string"},
        "scheduler": {"type": "string"},
    },
}


PROMPT_TEMPLATE = """あなたはStable Diffusion/ComfyUI向けの画像生成プロンプトエンジニアです。
次の漫画コマ情報を英語の画像生成プロンプトに変換してください。

条件:
- 出力はJSONのみ
- positive と negative を分ける
- セリフ、擬音、文字は画像に入れない
- キャラクター固定要素を先頭に置く
- 構図、表情、背景、光、漫画的演出を含める
- 1つのコマに情報を詰め込みすぎない

コマ情報:
{panel_plan}

キャラクター情報:
{characters}
"""


@dataclass
class PromptBuilder:
    provider: LLMProvider
    width: int = 1024
    height: int = 1024
    steps: int = 28
    cfg: float = 7.0
    sampler: str = "euler_ancestral"
    scheduler: str = "normal"
    quality_prompt: str = "masterpiece, best quality, highly detailed"
    style_prompt: str = "anime, comic style, clean lineart"
    max_repair_attempts: int = 1
    system_prompt: str = (
        "You are a Stable Diffusion prompt engineer. Always respond with strict JSON only."
    )
    seed_generator: random.Random = random.Random()

    def build_prompt(
        self,
        panel: PanelPlan | Mapping[str, Any] | str,
        characters: list[Mapping[str, Any]] | str | None = None,
    ) -> str:
        if isinstance(panel, PanelPlan):
            panel = panel.model_dump(mode="json")
        if isinstance(panel, Mapping):
            panel = json.dumps(panel, ensure_ascii=False, indent=2)
        if characters is None:
            characters = "[]"
        elif isinstance(characters, list):
            characters = json.dumps(characters, ensure_ascii=False, indent=2)
        return PROMPT_TEMPLATE.format(panel_plan=panel, characters=characters)

    async def build(
        self,
        panel: PanelPlan | Mapping[str, Any] | str,
        characters: list[Mapping[str, Any]] | str | None = None,
    ) -> PromptSpec:
        prompt = self.build_prompt(panel, characters)
        data = await self.provider.complete_json(
            prompt,
            schema=PROMPT_SPEC_SCHEMA,
            system=self.system_prompt,
            max_repair_attempts=self.max_repair_attempts,
        )
        outcome = validate_llm_output(data, jsonschema_definition=PROMPT_SPEC_SCHEMA)
        if not outcome.ok:
            raise ValueError(f"PromptSpec validation failed: {outcome.errors}")
        return self._to_model(data)

    def _to_model(self, data: Mapping[str, Any]) -> PromptSpec:
        seed = data.get("seed")
        if seed is None or seed == 0:
            seed = self.seed_generator.randint(1, 2**31 - 1)
        return PromptSpec(
            positive=data["positive"],
            negative=data["negative"],
            character_prompt=data.get("characterPrompt", ""),
            background_prompt=data.get("backgroundPrompt", ""),
            action_prompt=data.get("actionPrompt", ""),
            camera_prompt=data.get("cameraPrompt", ""),
            emotion_prompt=data.get("emotionPrompt", ""),
            style_prompt=data.get("stylePrompt", self.style_prompt),
            quality_prompt=data.get("qualityPrompt", self.quality_prompt),
            seed=int(seed),
            width=int(data.get("width", self.width)),
            height=int(data.get("height", self.height)),
            steps=int(data.get("steps", self.steps)),
            cfg=float(data.get("cfg", self.cfg)),
            sampler=data.get("sampler", self.sampler),
            scheduler=data.get("scheduler", self.scheduler),
        )


__all__ = [
    "PromptBuilder",
    "PromptSpec",
    "PROMPT_SPEC_SCHEMA",
    "PROMPT_TEMPLATE",
    "GLOBAL_NEGATIVE",
    "CHARACTER_NEGATIVE",
    "MANGA_NEGATIVE",
]
