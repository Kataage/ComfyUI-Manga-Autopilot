from __future__ import annotations

from manga_autopilot.models.scene_state import (
    CharacterSceneState,
    ObjectSceneState,
    SceneState,
    SceneStateDelta,
    StateEvent,
)
from manga_autopilot.services.scene_state_reducer import apply_scene_delta


def test_scene_state_round_trip_preserves_held_objects() -> None:
    state = SceneState(
        location="old_shrine",
        characters={
            "hero": CharacterSceneState(held_items=["umbrella"]),
        },
        objects={
            "umbrella": ObjectSceneState(owner="hero", state="open"),
        },
    )

    assert SceneState.model_validate_json(state.model_dump_json()) == state


def test_reducer_applies_ordered_location_and_object_events() -> None:
    state = SceneState(
        location="road",
        characters={"hero": CharacterSceneState()},
    )
    delta = SceneStateDelta(
        page_number=1,
        panel_number=2,
        events=[
            StateEvent(kind="set_location", value="old_shrine"),
            StateEvent(kind="acquire_object", character_id="hero", value="umbrella"),
            StateEvent(kind="set_object_state", value="umbrella", state="open"),
        ],
    )

    result = apply_scene_delta(state, delta, {"hero"})

    assert result.applied is True
    assert result.state.location == "old_shrine"
    assert result.state.characters["hero"].held_items == ["umbrella"]
    assert result.state.objects["umbrella"].owner == "hero"
    assert result.state.objects["umbrella"].state == "open"
    assert state.location == "road"


def test_transfer_requires_current_owner_and_is_atomic() -> None:
    state = SceneState(
        characters={
            "a": CharacterSceneState(),
            "b": CharacterSceneState(),
        }
    )
    delta = SceneStateDelta(
        events=[
            StateEvent(
                kind="transfer_object",
                character_id="a",
                target_character_id="b",
                value="key",
            )
        ]
    )

    result = apply_scene_delta(state, delta, {"a", "b"})

    assert result.applied is False
    assert result.warnings[0].code == "impossible_ownership"
    assert result.state == state


def test_unknown_character_blocks_delta() -> None:
    state = SceneState(characters={"hero": CharacterSceneState()})
    delta = SceneStateDelta(
        events=[
            StateEvent(kind="set_emotion", character_id="stranger", value="happy")
        ]
    )

    result = apply_scene_delta(state, delta, {"hero"})

    assert result.applied is False
    assert result.warnings[0].code == "unknown_character"


def test_unexplained_clothing_change_warns_but_applies() -> None:
    state = SceneState(
        characters={"hero": CharacterSceneState(clothing="school_uniform")}
    )
    delta = SceneStateDelta(
        events=[
            StateEvent(kind="set_clothing", character_id="hero", value="raincoat")
        ]
    )

    result = apply_scene_delta(state, delta, {"hero"})

    assert result.applied is True
    assert result.state.characters["hero"].clothing == "raincoat"
    assert result.warnings[0].code == "unexplained_clothing_change"
    assert result.warnings[0].severity == "warning"


def test_panel_plan_accepts_scene_delta_without_breaking_old_defaults() -> None:
    from manga_autopilot.models.page import PanelPlan

    old = PanelPlan(panel_number=1)
    changed = PanelPlan(
        panel_number=2,
        layout_id="page_2_horizontal",
        scene_delta=SceneStateDelta(
            events=[StateEvent(kind="set_time", value="evening")]
        ),
    )

    assert old.scene_delta.events == []
    assert old.layout_id is None
    assert changed.scene_delta.events[0].value == "evening"

