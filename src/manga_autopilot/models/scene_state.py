"""Persisted scene state and LLM-authored delta models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CharacterSceneState(BaseModel):
    clothing: str = ""
    emotion: str = ""
    position: str = ""
    held_items: list[str] = Field(default_factory=list)


class ObjectSceneState(BaseModel):
    owner: str | None = None
    state: str = ""


class SceneState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    location: str = ""
    time: str = ""
    weather: str = ""
    characters: dict[str, CharacterSceneState] = Field(default_factory=dict)
    objects: dict[str, ObjectSceneState] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0)


StateEventKind = Literal[
    "set_location",
    "set_time",
    "set_weather",
    "set_emotion",
    "set_position",
    "set_clothing",
    "acquire_object",
    "drop_object",
    "transfer_object",
    "set_object_state",
]


class StateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: StateEventKind
    character_id: str | None = None
    target_character_id: str | None = None
    value: str = ""
    state: str = ""
    reason: str = ""


class SceneStateDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int | None = Field(default=None, ge=1)
    panel_number: int | None = Field(default=None, ge=1)
    events: list[StateEvent] = Field(default_factory=list)


class StateWarning(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error"] = "warning"
    event_index: int | None = None


__all__ = [
    "CharacterSceneState",
    "ObjectSceneState",
    "SceneState",
    "SceneStateDelta",
    "StateEvent",
    "StateEventKind",
    "StateWarning",
]
