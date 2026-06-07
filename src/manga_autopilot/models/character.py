"""Character data models (spec sections 13.1-13.8).

Models the on-disk representation of a story character: identity, appearance,
outfit, colour palette, reference / expression asset references, optional
LoRA / IP-Adapter refs, and the consistency prompts used by image generation.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Role = Literal["protagonist", "heroine", "villain", "support", "mob"]
GenderExpression = Literal["masculine", "feminine", "androgynous", "ambiguous", "non_binary"]


class AssetRef(BaseModel):
    """Reference to a project asset (image / video / audio)."""

    asset_id: str = Field(min_length=1, max_length=128)
    kind: Literal["image", "video", "audio"] = "image"
    path: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=128)


class LoraRef(BaseModel):
    """Optional LoRA reference attached to a character."""

    name: str = Field(min_length=1, max_length=128)
    strength_model: float = Field(default=0.85, ge=-2.0, le=2.0)
    strength_clip: float = Field(default=0.85, ge=-2.0, le=2.0)


class ColorPalette(BaseModel):
    """Hex colours for a character's primary visual identity."""

    primary: str
    secondary: str = "#000000"
    accent: str = "#000000"
    skin: str = "#f0d0b0"
    hair: str = "#000000"
    eyes: str = "#000000"

    @field_validator("primary", "secondary", "accent", "skin", "hair", "eyes")
    @classmethod
    def _validate_hex(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError(f"colour must be #RRGGBB, got {value!r}")
        return value.lower()


class CharacterAppearance(BaseModel):
    """Spec 13.2."""

    gender_expression: GenderExpression | None = None
    hair_color: str = Field(min_length=1, max_length=64)
    hair_style: str = Field(min_length=1, max_length=128)
    eye_color: str = Field(min_length=1, max_length=64)
    face_features: list[str] = Field(default_factory=list, max_length=64)
    body_type: str = Field(default="", max_length=128)
    height: str = Field(default="", max_length=64)
    distinctive_features: list[str] = Field(default_factory=list, max_length=32)


class Outfit(BaseModel):
    """Spec 13.3."""

    base: str = Field(default="", max_length=256)
    upper: str = Field(default="", max_length=128)
    lower: str = Field(default="", max_length=128)
    shoes: str = Field(default="", max_length=128)
    accessories: list[str] = Field(default_factory=list, max_length=32)
    weapon: str = Field(default="", max_length=128)
    must_keep: list[str] = Field(default_factory=list, max_length=64)
    must_avoid: list[str] = Field(default_factory=list, max_length=64)


class Character(BaseModel):
    """Spec 13.1 — full character record persisted in ``character_card.json``."""

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    role: Role = "support"
    description: str = Field(default="", max_length=1024)
    personality: str = Field(default="", max_length=512)
    age_appearance: str = Field(default="", max_length=64)
    appearance: CharacterAppearance
    outfit: Outfit = Field(default_factory=Outfit)
    color_palette: ColorPalette
    reference_images: list[AssetRef] = Field(default_factory=list, max_length=32)
    expression_images: list[AssetRef] = Field(default_factory=list, max_length=32)
    lora: LoraRef | None = None
    ip_adapter_ref: AssetRef | None = None
    consistency_prompt: str = Field(default="", max_length=1024)
    negative_prompt: str = Field(default="", max_length=512)
    fixed: bool = True
    consistency_level: int = Field(default=1, ge=1, le=7)

    @field_validator("id")
    @classmethod
    def _id_slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9_\-]+", value):
            raise ValueError(f"character id must be a slug (got {value!r})")
        return value

    def must_keep_combined(self) -> list[str]:
        """Combine outfit must_keep + consistency_prompt tokens (deduplicated)."""

        out: list[str] = []
        seen: set[str] = set()
        for item in self.outfit.must_keep:
            key = item.strip().lower()
            if key and key not in seen:
                out.append(item.strip())
                seen.add(key)
        return out

    def must_avoid_combined(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in self.outfit.must_avoid:
            key = item.strip().lower()
            if key and key not in seen:
                out.append(item.strip())
                seen.add(key)
        return out


__all__ = [
    "AssetRef",
    "LoraRef",
    "ColorPalette",
    "CharacterAppearance",
    "Outfit",
    "Character",
]
