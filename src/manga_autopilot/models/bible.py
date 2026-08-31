"""Story and dialogue continuity models for Anima planning."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CharacterSpeech(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: str = Field(default="", max_length=128)
    sentence_length: str = Field(default="", max_length=64)
    common_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)


class StoryBible(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="", max_length=256)
    genre: str = Field(default="", max_length=128)
    tone: str = Field(default="", max_length=128)
    theme: str = Field(default="", max_length=256)
    world: str = Field(default="", max_length=4096)
    rules: list[str] = Field(default_factory=list)
    timeline: list[str] = Field(default_factory=list)
    locations: dict[str, str] = Field(default_factory=dict)
    important_objects: dict[str, str] = Field(default_factory=dict)
    relationships: list[str] = Field(default_factory=list)
    foreshadowing: list[str] = Field(default_factory=list)
    resolved_events: list[str] = Field(default_factory=list)
    unresolved_events: list[str] = Field(default_factory=list)


__all__ = ["CharacterSpeech", "StoryBible"]
