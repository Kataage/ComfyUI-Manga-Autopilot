"""Story / Act / StoryPlan models (spec section 14.2-14.4)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from manga_autopilot.models.page import PagePlan


class Act(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    start_page: int = Field(ge=1, le=9999)
    end_page: int = Field(ge=1, le=9999)
    summary: str = Field(default="", max_length=1024)
    emotional_arc: str = Field(default="", max_length=256)


class StoryPlan(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    logline: str = Field(default="", max_length=512)
    theme: str = Field(default="", max_length=256)
    genre: str = Field(default="", max_length=128)
    mood: str = Field(default="", max_length=128)
    acts: list[Act] = Field(default_factory=list)
    pages: list[PagePlan] = Field(default_factory=list)


__all__ = ["Act", "StoryPlan"]
