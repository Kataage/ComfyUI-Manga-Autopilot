"""Speech bubble + font spec models (spec sections 19.2 and 19.3)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

BUBBLE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


BubbleType = Literal["normal", "shout", "thought", "narration", "whisper", "radio"]
BubbleDirection = Literal["vertical", "horizontal"]
FontWeight = Literal["normal", "bold"]


class FontSpec(BaseModel):
    family: str = Field(default="NotoSansJP", min_length=1, max_length=64)
    size: float = Field(default=18.0, gt=0.0, le=512.0)
    weight: FontWeight = "normal"
    line_height: float = Field(default=1.4, ge=0.5, le=4.0)
    letter_spacing: float = Field(default=0.0, ge=-10.0, le=20.0)
    color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")


class TailTarget(BaseModel):
    x: float
    y: float


class SpeechBubble(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    panel_id: str = Field(min_length=1, max_length=64)
    type: BubbleType = "normal"
    text: str = Field(default="", max_length=1024)
    x: float = 0.0
    y: float = 0.0
    width: float = Field(default=160.0, gt=0.0)
    height: float = Field(default=80.0, gt=0.0)
    tail_target: TailTarget | None = None
    font: FontSpec = Field(default_factory=FontSpec)
    direction: BubbleDirection = "vertical"
    order: int = 0

    @field_validator("id", "panel_id")
    @classmethod
    def _check_ids(cls, value: str) -> str:
        if not BUBBLE_ID_RE.fullmatch(value):
            raise ValueError("id and panel_id must match ^[A-Za-z0-9_-]{1,64}$")
        return value

    def character_count(self) -> int:
        return len(self.text)


def bubble_storage_filename() -> str:
    """Return the canonical filename for persisted bubbles."""

    return "bubbles.json"


__all__ = [
    "BUBBLE_ID_RE",
    "BubbleType",
    "BubbleDirection",
    "FontWeight",
    "FontSpec",
    "TailTarget",
    "SpeechBubble",
    "bubble_storage_filename",
]
