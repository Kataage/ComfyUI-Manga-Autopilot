"""Page, Panel, Dialogue, SFX Pydantic models (spec section 14.4 / 14.5)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, NonNegativeInt, field_validator

from manga_autopilot.models.scene_state import SceneStateDelta


# ----------------------------------------------------------------- Dialogue
class Dialogue(BaseModel):
    character_id: str | None = None
    speaker: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    type: Literal["speech", "thought", "narration", "whisper"] = "speech"
    balloon_position: Literal["top", "middle", "bottom", "free"] = "top"


# ----------------------------------------------------------------- SFX
class SoundEffect(BaseModel):
    text: str = Field(min_length=1, max_length=64)
    intensity: NonNegativeInt = 0
    position: Literal["top", "middle", "bottom", "free"] = "free"


# ----------------------------------------------------------------- Panel
VisualPriority = Literal["character", "action", "background", "emotion"]


class PanelPlan(BaseModel):
    panel_number: int = Field(ge=1, le=99)
    purpose: str = Field(default="", max_length=512)
    shot: str = Field(default="", max_length=64)
    camera_angle: str = Field(default="", max_length=64)
    characters: list[str] = Field(default_factory=list)
    background: str = Field(default="", max_length=256)
    action: str = Field(default="", max_length=512)
    emotion: str = Field(default="", max_length=256)
    visual_priority: VisualPriority = "character"
    dialogue: list[Dialogue] = Field(default_factory=list)
    sfx: list[SoundEffect] = Field(default_factory=list)
    layout_id: str | None = Field(default=None, max_length=64)
    scene_delta: SceneStateDelta = Field(default_factory=SceneStateDelta)

    @field_validator("characters")
    @classmethod
    def _unique_characters(cls, value: list[str]) -> list[str]:
        # Preserve order while removing duplicates.
        seen: set[str] = set()
        out: list[str] = []
        for name in value:
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out


# ----------------------------------------------------------------- Page
class PagePlan(BaseModel):
    page_number: int = Field(ge=1, le=9999)
    summary: str = Field(default="", max_length=1024)
    emotional_goal: str = Field(default="", max_length=256)
    visual_goal: str = Field(default="", max_length=256)
    panel_count: int = Field(ge=1, le=24)
    cliffhanger: str | None = Field(default=None, max_length=512)
    layout_id: str | None = Field(default=None, max_length=64)
    panels: list[PanelPlan] = Field(default_factory=list)

    @field_validator("panels")
    @classmethod
    def _panels_match_count(cls, value: list[PanelPlan], info) -> list[PanelPlan]:
        count = info.data.get("panel_count") if hasattr(info, "data") else None
        if count is not None and len(value) > count:
            raise ValueError(
                f"page declares {count} panels but {len(value)} panel definitions were provided"
            )
        return value


# ----------------------------------------------------------------- Project
class ProjectMetadata(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    synopsis: str = Field(default="", max_length=4096)
    style: str = Field(default="", max_length=128)
    target_pages: int = Field(default=4, ge=1, le=999)
    characters: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4096)
    created_at: datetime = Field(default_factory=lambda: datetime.utcnow())
    updated_at: datetime = Field(default_factory=lambda: datetime.utcnow())


__all__ = [
    "Dialogue",
    "SoundEffect",
    "PanelPlan",
    "PagePlan",
    "ProjectMetadata",
    "VisualPriority",
]
