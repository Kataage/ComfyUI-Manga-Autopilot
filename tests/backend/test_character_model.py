"""Tests for the Character data model (spec section 13)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from manga_autopilot.models.character import (
    AssetRef,
    Character,
    CharacterAppearance,
    ColorPalette,
    LoraRef,
    Outfit,
)


def _appearance() -> CharacterAppearance:
    return CharacterAppearance(
        hair_color="silver",
        hair_style="long",
        eye_color="blue",
    )


def test_character_minimal() -> None:
    char = Character(
        id="alice",
        name="Alice",
        appearance=_appearance(),
        color_palette=ColorPalette(primary="#102030"),
    )
    assert char.role == "support"
    assert char.fixed is True
    assert char.consistency_level == 1
    assert char.outfit.must_keep == []


def test_character_id_must_be_slug() -> None:
    with pytest.raises(ValidationError):
        Character(
            id="Alice Bob",
            name="A",
            appearance=_appearance(),
            color_palette=ColorPalette(primary="#000000"),
        )


def test_color_palette_validates_hex() -> None:
    with pytest.raises(ValidationError):
        ColorPalette(primary="red")


def test_outfit_must_keep_preserved() -> None:
    outfit = Outfit(
        base="dress",
        must_keep=["long hair", "blue eyes"],
        must_avoid=["short hair"],
    )
    assert outfit.must_keep == ["long hair", "blue eyes"]


def test_must_keep_combined_dedupes() -> None:
    char = Character(
        id="bob",
        name="Bob",
        appearance=_appearance(),
        outfit=Outfit(must_keep=["silver hair", "silver hair", "blue eyes"]),
        color_palette=ColorPalette(primary="#000000"),
    )
    combined = char.must_keep_combined()
    assert combined == ["silver hair", "blue eyes"]


def test_must_avoid_combined_dedupes() -> None:
    char = Character(
        id="bob",
        name="Bob",
        appearance=_appearance(),
        outfit=Outfit(must_avoid=["short hair", "short hair"]),
        color_palette=ColorPalette(primary="#000000"),
    )
    assert char.must_avoid_combined() == ["short hair"]


def test_asset_ref_defaults_kind() -> None:
    ref = AssetRef(asset_id="x", path="assets/x.png")
    assert ref.kind == "image"


def test_lora_ref_strengths() -> None:
    lora = LoraRef(name="alice", strength_model=0.7, strength_clip=0.6)
    assert lora.strength_model == 0.7
    with pytest.raises(ValidationError):
        LoraRef(name="x", strength_model=5.0)


def test_character_round_trip_json() -> None:
    char = Character(
        id="kira",
        name="Kira",
        role="protagonist",
        description="main hero",
        appearance=_appearance(),
        outfit=Outfit(must_keep=["silver long hair", "blue eyes"]),
        color_palette=ColorPalette(primary="#a0b0c0", hair="#c0c0c0", eyes="#3050ff"),
    )
    data = char.model_dump(mode="json")
    restored = Character.model_validate(data)
    assert restored == char


#: A literal Windows separator, built without an escape sequence.
SEP = chr(92)


def test_asset_ref_path_is_stored_posix_separated() -> None:
    """A project written on Windows must still resolve elsewhere."""
    from manga_autopilot.models.character import AssetRef

    windows_path = SEP.join(["assets", "characters", "alice", "ref_001.png"])

    ref = AssetRef(asset_id="ref_001", path=windows_path)

    assert ref.path == "assets/characters/alice/ref_001.png"
    assert SEP not in ref.model_dump_json()


def test_asset_ref_leaves_a_posix_path_alone() -> None:
    from manga_autopilot.models.character import AssetRef

    ref = AssetRef(asset_id="ref_001", path="assets/characters/alice/ref_001.png")

    assert ref.path == "assets/characters/alice/ref_001.png"
