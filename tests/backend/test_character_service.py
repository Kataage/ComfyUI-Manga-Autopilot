"""Tests for the character service (spec section 13, 22)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from manga_autopilot.models.character import (
    AssetRef,
    Character,
    CharacterAppearance,
    ColorPalette,
    LoraRef,
    Outfit,
)
from manga_autopilot.services.character_service import (
    SHEET_VIEWS,
    CharacterNotFoundError,
    CharacterService,
    CharacterValidationError,
    build_character_negative,
    build_character_prompt,
    build_ip_adapter_overrides,
    build_lora_overrides,
    expression_presets,
    is_valid_expression,
    is_valid_pose,
    pose_presets,
    sheet_prompt_for_view,
)


def _appearance() -> CharacterAppearance:
    return CharacterAppearance(hair_color="silver", hair_style="long", eye_color="blue")


def _char(cid: str = "alice", **kwargs) -> Character:
    return Character(
        id=cid,
        name=cid.title(),
        appearance=_appearance(),
        color_palette=ColorPalette(primary="#102030"),
        **kwargs,
    )


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def test_list_empty(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    assert svc.list() == []


def test_create_and_get(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    char = _char()
    svc.create(char)
    fetched = svc.get("alice")
    assert fetched.id == "alice"
    # Persisted to disk
    raw = json.loads((tmp_path / "projects" / "proj-1" / "characters.json").read_text())
    assert raw[0]["id"] == "alice"


def test_create_duplicate_raises(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char())
    with pytest.raises(CharacterValidationError):
        svc.create(_char())


def test_update_and_delete(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char(outfit=Outfit(base="armor")))
    updated = svc.update("alice", {"outfit": Outfit(base="cloak")})
    assert updated.outfit.base == "cloak"
    svc.delete("alice")
    with pytest.raises(CharacterNotFoundError):
        svc.get("alice")
    with pytest.raises(CharacterNotFoundError):
        svc.delete("alice")


def test_update_with_model(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char())
    new_char = _char(description="updated")
    out = svc.update("alice", new_char)
    assert out.description == "updated"


def test_register_reference_image(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char())
    upload = svc.register_reference_image("alice", "ref.png", _png_bytes(), label="hero")
    assert upload.width == 32 and upload.height == 32
    assert upload.asset_ref.path.startswith("assets/characters/alice/")
    assert upload.stored_path.exists()


def test_register_reference_rejects_bad_image(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char())
    with pytest.raises(CharacterValidationError):
        svc.register_reference_image("alice", "bad.png", b"not an image")


def test_register_reference_rejects_bad_extension(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char())
    with pytest.raises(CharacterValidationError):
        svc.register_reference_image("alice", "bad.txt", _png_bytes())


def test_register_reference_too_many(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char())
    for i in range(8):
        svc.register_reference_image("alice", f"ref_{i}.png", _png_bytes())
    with pytest.raises(CharacterValidationError):
        svc.register_reference_image("alice", "extra.png", _png_bytes())


def test_character_card_written(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char())
    card = svc.write_character_card("alice", sheet_paths={"front": "assets/characters/alice/reference_front.png"})
    assert card.exists()
    data = json.loads(card.read_text())
    assert data["sheet_paths"]["front"].endswith("reference_front.png")


def test_sheet_targets(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    targets = svc.sheet_targets("alice")
    assert set(targets) == set(SHEET_VIEWS)
    for p in targets.values():
        assert p.name.startswith("reference_")


def test_build_character_prompt_locked() -> None:
    char = _char(
        outfit=Outfit(
            base="armor",
            must_keep=["silver long hair", "blue eyes"],
            must_avoid=["short hair"],
        )
    )
    prompt = build_character_prompt(char)
    assert prompt.startswith("silver long hair, blue eyes")


def test_build_character_negative() -> None:
    char = _char(outfit=Outfit(must_avoid=["short hair", "red hair"]))
    neg = build_character_negative(char)
    assert "short hair" in neg and "red hair" in neg
    assert "low quality" in neg


def test_build_lora_overrides() -> None:
    char = _char(lora=LoraRef(name="alice", strength_model=0.7, strength_clip=0.5))
    out = build_lora_overrides(char)
    assert out == {
        "lora_name": "alice",
        "lora_strength_model": 0.7,
        "lora_strength_clip": 0.5,
    }


def test_build_lora_overrides_none() -> None:
    char = _char()
    assert build_lora_overrides(char) == {}


def test_build_ip_adapter_overrides() -> None:
    char = _char(ip_adapter_ref=AssetRef(asset_id="ip", path="assets/characters/alice/ip.png"))
    out = build_ip_adapter_overrides(char)
    assert out["ip_adapter_image"].endswith("ip.png")
    assert out["ip_adapter_strength"] == 0.8


def test_build_ip_adapter_overrides_none() -> None:
    char = _char()
    assert build_ip_adapter_overrides(char) == {}


def test_expression_and_pose_presets() -> None:
    assert "smile" in expression_presets()
    assert "neutral" in expression_presets()
    assert "standing" in pose_presets()
    assert is_valid_expression("smile")
    assert not is_valid_expression("lol")
    assert is_valid_pose("standing")
    assert not is_valid_pose("flying")


def test_sheet_prompt_for_view() -> None:
    char = _char()
    for view in SHEET_VIEWS:
        prompt = sheet_prompt_for_view(char, view)
        assert "silver" in prompt or "blue" in prompt
    with pytest.raises(ValueError):
        sheet_prompt_for_view(char, "bogus")


def test_consistency_prompt_too_long(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    svc.create(_char())
    with pytest.raises(CharacterValidationError):
        svc.update("alice", {"consistency_prompt": "x" * 2000})


def test_character_not_found(tmp_path: Path) -> None:
    svc = CharacterService.for_project(tmp_path, "proj-1")
    with pytest.raises(CharacterNotFoundError):
        svc.get("ghost")
