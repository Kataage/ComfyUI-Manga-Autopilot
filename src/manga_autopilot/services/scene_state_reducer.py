"""Pure, deterministic reducer for panel-level scene state deltas."""

from __future__ import annotations

from dataclasses import dataclass

from manga_autopilot.models.scene_state import (
    CharacterSceneState,
    ObjectSceneState,
    SceneState,
    SceneStateDelta,
    StateEvent,
    StateWarning,
)


@dataclass(frozen=True)
class ReductionResult:
    state: SceneState
    applied: bool
    warnings: list[StateWarning]


def apply_scene_delta(
    state: SceneState,
    delta: SceneStateDelta,
    known_character_ids: set[str],
) -> ReductionResult:
    original = state.model_copy(deep=True)
    working = state.model_copy(deep=True)
    warnings: list[StateWarning] = []

    for index, event in enumerate(delta.events):
        error = _apply_event(working, event, known_character_ids, index, warnings)
        if error is not None:
            warnings.append(error)
            return ReductionResult(state=original, applied=False, warnings=warnings)

    working.revision += 1
    return ReductionResult(state=working, applied=True, warnings=warnings)


def _apply_event(
    state: SceneState,
    event: StateEvent,
    known_character_ids: set[str],
    index: int,
    warnings: list[StateWarning],
) -> StateWarning | None:
    if event.kind == "set_location":
        state.location = event.value
        return None
    if event.kind == "set_time":
        state.time = event.value
        return None
    if event.kind == "set_weather":
        state.weather = event.value
        return None
    if event.kind == "set_object_state":
        item = event.value
        state.objects.setdefault(item, ObjectSceneState()).state = event.state
        return None

    character_error = _require_character(
        state,
        event.character_id,
        known_character_ids,
        index,
    )
    if character_error is not None:
        return character_error
    character_id = event.character_id or ""
    character = state.characters[character_id]

    if event.kind == "set_emotion":
        character.emotion = event.value
    elif event.kind == "set_position":
        character.position = event.value
    elif event.kind == "set_clothing":
        if character.clothing and character.clothing != event.value and not event.reason:
            warnings.append(
                StateWarning(
                    code="unexplained_clothing_change",
                    message=(
                        f"{character_id!r} changes clothing from "
                        f"{character.clothing!r} to {event.value!r} without a reason"
                    ),
                    event_index=index,
                )
            )
        character.clothing = event.value
    elif event.kind == "acquire_object":
        item = event.value
        obj = state.objects.setdefault(item, ObjectSceneState())
        if obj.owner and obj.owner != character_id:
            return _ownership_error(item, character_id, obj.owner, index)
        obj.owner = character_id
        if item not in character.held_items:
            character.held_items.append(item)
    elif event.kind == "drop_object":
        item = event.value
        obj = state.objects.get(item)
        if obj is None or obj.owner != character_id or item not in character.held_items:
            return _ownership_error(item, character_id, obj.owner if obj else None, index)
        obj.owner = None
        character.held_items.remove(item)
    elif event.kind == "transfer_object":
        target_error = _require_character(
            state,
            event.target_character_id,
            known_character_ids,
            index,
        )
        if target_error is not None:
            return target_error
        item = event.value
        obj = state.objects.get(item)
        if obj is None or obj.owner != character_id or item not in character.held_items:
            return _ownership_error(item, character_id, obj.owner if obj else None, index)
        target_id = event.target_character_id or ""
        character.held_items.remove(item)
        target = state.characters[target_id]
        if item not in target.held_items:
            target.held_items.append(item)
        obj.owner = target_id
    return None


def _require_character(
    state: SceneState,
    character_id: str | None,
    known_character_ids: set[str],
    event_index: int,
) -> StateWarning | None:
    if not character_id or character_id not in known_character_ids:
        return StateWarning(
            code="unknown_character",
            message=f"character {character_id!r} is not defined",
            severity="error",
            event_index=event_index,
        )
    state.characters.setdefault(character_id, CharacterSceneState())
    return None


def _ownership_error(
    item: str,
    expected_owner: str,
    actual_owner: str | None,
    event_index: int,
) -> StateWarning:
    return StateWarning(
        code="impossible_ownership",
        message=(
            f"object {item!r} is not held by {expected_owner!r}; "
            f"current owner is {actual_owner!r}"
        ),
        severity="error",
        event_index=event_index,
    )


__all__ = ["ReductionResult", "apply_scene_delta"]
