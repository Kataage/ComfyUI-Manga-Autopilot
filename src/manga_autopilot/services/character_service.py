"""Character service (spec sections 13.1-13.8, 22 Character Manager).

This module provides:

- :class:`CharacterService` - project-scoped CRUD persistence for characters
  (id slug validation, per-project ``characters.json``).
- :func:`SHEET_VIEWS` - the canonical set of sheet views (front/side/back/face/
  expression/outfit) per spec 13.5.
- :class:`ExpressionPresets` / :func:`EXPRESSION_PRESETS` / :func:`POSE_PRESETS`
  per spec 13.7/13.8.
- :func:`build_character_prompt` - LLM-ready prompt assembly with mustKeep /
  mustAvoid locking (spec 13.6).
- :func:`build_lora_overrides` / :func:`build_ip_adapter_overrides` - workflow
  binding helpers for the LoRA / IP-Adapter paths (spec 13.4 levels 4 and 5).
- :class:`ReferenceUpload` - a small dataclass returned by
  :meth:`CharacterService.register_reference_image` for spec 13.4 level 3.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from manga_autopilot.models.character import (
    AssetRef,
    Character,
)
from manga_autopilot.storage.paths import ensure_project_paths

log = logging.getLogger(__name__)

CHARACTERS_FILENAME = "characters.json"
CHARACTER_CARD_FILENAME = "character_card.json"
ALLOWED_IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
MAX_REFERENCE_IMAGES = 8
MAX_EXPRESSION_IMAGES = 16

SHEET_VIEWS: tuple[str, ...] = (
    "front",
    "side",
    "back",
    "face",
    "expression",
    "outfit",
)

EXPRESSION_PRESETS: tuple[str, ...] = (
    "neutral",
    "smile",
    "angry",
    "sad",
    "crying",
    "surprised",
    "determined",
    "embarrassed",
    "fear",
    "pain",
    "shouting",
    "relieved",
    "confused",
    "serious",
    "despair",
)

POSE_PRESETS: tuple[str, ...] = (
    "standing",
    "running",
    "walking",
    "falling",
    "kneeling",
    "looking back",
    "holding sword",
    "battle stance",
    "reaching hand",
    "turning around",
    "close-up face",
    "upper body shot",
    "from behind",
    "low angle",
    "high angle",
)


class CharacterNotFoundError(Exception):
    pass


class CharacterValidationError(Exception):
    pass


@dataclass
class ReferenceUpload:
    """Result returned from :meth:`CharacterService.register_reference_image`."""

    asset_ref: AssetRef
    stored_path: Path
    width: int
    height: int
    bytes_written: int


@dataclass
class CharacterService:
    """Project-scoped character CRUD + asset registration."""

    project_root: Path
    storage_root: Path | None = None
    _cache: list[Character] = field(default_factory=list, init=False)
    _loaded: bool = field(default=False, init=False)

    @classmethod
    def for_project(cls, storage_root: str | Path, project_id: str) -> CharacterService:
        paths = ensure_project_paths(storage_root, project_id)
        return cls(
            project_root=paths.root,
            storage_root=Path(storage_root).expanduser().resolve(),
        )

    @classmethod
    def from_paths(cls, paths_path: str | Path) -> CharacterService:
        p = Path(paths_path)
        # Expect {storage_root}/projects/{project_id}
        if p.parent.name == "projects" and p.parent.parent is not None:
            return cls(project_root=p, storage_root=p.parent.parent)
        return cls(project_root=p, storage_root=p)

    @property
    def characters_file(self) -> Path:
        return self.project_root / CHARACTERS_FILENAME

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self.characters_file.exists():
            data = json.loads(self.characters_file.read_text(encoding="utf-8"))
            self._cache = [Character.model_validate(c) for c in data]
        else:
            self._cache = []
        self._loaded = True

    def list(self) -> list[Character]:
        self._ensure_loaded()
        return list(self._cache)

    def get(self, character_id: str) -> Character:
        self._ensure_loaded()
        for char in self._cache:
            if char.id == character_id:
                return char
        raise CharacterNotFoundError(f"character not found: {character_id}")

    def _persist(self) -> None:
        data = [c.model_dump(mode="json") for c in self._cache]
        self.characters_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create(self, character: Character) -> Character:
        self._ensure_loaded()
        if any(c.id == character.id for c in self._cache):
            raise CharacterValidationError(f"character id already exists: {character.id}")
        self._validate_consistency_prompt(character.consistency_prompt)
        self._cache.append(character)
        self._persist()
        return character

    def update(self, character_id: str, patch: Mapping[str, Any] | Character) -> Character:
        self._ensure_loaded()
        idx = next(
            (i for i, c in enumerate(self._cache) if c.id == character_id),
            None,
        )
        if idx is None:
            raise CharacterNotFoundError(character_id)
        current = self._cache[idx]
        patch_data = patch.model_dump(mode="json") if isinstance(patch, Character) else dict(patch)
        self._validate_consistency_prompt(patch_data.get("consistency_prompt", current.consistency_prompt))
        merged = current.model_copy(update=patch_data)
        self._cache[idx] = merged
        self._persist()
        return merged

    def delete(self, character_id: str) -> None:
        self._ensure_loaded()
        before = len(self._cache)
        self._cache = [c for c in self._cache if c.id != character_id]
        if len(self._cache) == before:
            raise CharacterNotFoundError(character_id)
        self._persist()

    def _validate_consistency_prompt(self, prompt: str) -> None:
        if not prompt.strip():
            return
        if ";;;" in prompt:
            raise CharacterValidationError("consistency_prompt contains illegal sequence ';;;'")
        if len(prompt) > 1024:
            raise CharacterValidationError("consistency_prompt exceeds 1024 chars")

    # ----------------------------------------------------------------- assets
    def register_reference_image(
        self,
        character_id: str,
        filename: str,
        data: bytes,
        *,
        kind: str = "image",
        label: str = "",
    ) -> ReferenceUpload:
        """Store a reference image on disk and link it to the character."""

        char = self.get(character_id)
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_IMAGE_SUFFIXES:
            raise CharacterValidationError(f"unsupported image extension: {suffix}")
        if len(data) > 25 * 1024 * 1024:
            raise CharacterValidationError("reference image exceeds 25 MiB")

        try:
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
                width, height = img.size
        except Exception as exc:  # pragma: no cover - PIL raises a wide variety
            raise CharacterValidationError(f"invalid image data: {exc}") from exc

        # Re-open to get a clean size (verify() invalidates the file pointer)
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            mode = img.mode

        char_dir = self._character_dir(character_id)
        char_dir.mkdir(parents=True, exist_ok=True)
        target = char_dir / f"ref_{len(char.reference_images) + 1:03d}{suffix}"
        target.write_bytes(data)

        asset_ref = AssetRef(
            asset_id=target.stem,
            kind=kind,
            path=target.relative_to(self.project_root).as_posix(),
            label=label or target.stem,
        )
        new_refs = list(char.reference_images) + [asset_ref]
        if len(new_refs) > MAX_REFERENCE_IMAGES:
            raise CharacterValidationError(
                f"character {character_id} already has {MAX_REFERENCE_IMAGES} reference images"
            )
        self.update(character_id, {"reference_images": new_refs})
        log.info("stored %s (%dx%d mode=%s) for %s", target, width, height, mode, character_id)
        return ReferenceUpload(
            asset_ref=asset_ref,
            stored_path=target,
            width=width,
            height=height,
            bytes_written=len(data),
        )

    def write_character_card(
        self,
        character_id: str,
        *,
        sheet_paths: Mapping[str, str] | None = None,
    ) -> Path:
        """Write ``assets/characters/{id}/character_card.json`` (spec 13.5)."""

        char = self.get(character_id)
        char_dir = self._character_dir(character_id)
        char_dir.mkdir(parents=True, exist_ok=True)
        card_path = char_dir / CHARACTER_CARD_FILENAME
        card = char.model_dump(mode="json")
        card["sheet_paths"] = dict(sheet_paths or {})
        card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        return card_path

    # ----------------------------------------------------------- sheet views
    def sheet_targets(self, character_id: str) -> dict[str, Path]:
        """Return the expected on-disk paths for a character's sheet images."""

        char_dir = self._character_dir(character_id)
        return {view: char_dir / f"reference_{view}.png" for view in SHEET_VIEWS}

    def _character_dir(self, character_id: str) -> Path:
        return self.project_root / "assets" / "characters" / character_id


# --------------------------------------------------------------- prompt lock
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def build_character_prompt(character: Character, *, include_negative: bool = True) -> str:
    """Assemble an LLM-ready prompt for a character (spec 13.6).

    The output puts the ``mustKeep`` tokens first, then appearance, then
    outfit, then ``consistency_prompt``.  ``mustAvoid`` tokens are added to
    the negative prompt.
    """

    parts: list[str] = []
    parts.extend(character.must_keep_combined())
    parts.append(f"{character.appearance.hair_color} {character.appearance.hair_style} hair")
    parts.append(f"{character.appearance.eye_color} eyes")
    for feat in character.appearance.face_features:
        if feat:
            parts.append(feat)
    for feat in character.appearance.distinctive_features:
        if feat:
            parts.append(feat)
    outfit = character.outfit
    outfit_parts: list[str] = []
    for piece in (outfit.base, outfit.upper, outfit.lower, outfit.shoes):
        if piece:
            outfit_parts.append(piece)
    if outfit.weapon:
        outfit_parts.append(f"holding {outfit.weapon}")
    outfit_parts.extend(outfit.accessories)
    parts.extend(outfit_parts)
    if character.consistency_prompt:
        parts.append(character.consistency_prompt)
    positive = ", ".join(p for p in parts if p)
    if not include_negative:
        return positive
    return positive


def build_character_negative(character: Character) -> str:
    """Assemble a negative prompt enforcing ``mustAvoid`` tokens (spec 13.6)."""

    tokens = character.must_avoid_combined()
    base = "low quality, worst quality, blurry, bad anatomy, bad hands, extra fingers"
    return ", ".join([base, *tokens])


# --------------------------------------------------------------- LoRA / IP-A
def build_lora_overrides(character: Character) -> dict[str, Any]:
    """Workflow overrides that set a character's LoRA strength."""

    if not character.lora:
        return {}
    return {
        "lora_name": character.lora.name,
        "lora_strength_model": character.lora.strength_model,
        "lora_strength_clip": character.lora.strength_clip,
    }


def build_ip_adapter_overrides(character: Character) -> dict[str, Any]:
    """Workflow overrides that set IP-Adapter image + strength."""

    if not character.ip_adapter_ref:
        return {}
    return {
        "ip_adapter_image": character.ip_adapter_ref.path,
        "ip_adapter_strength": 0.8,
    }


# --------------------------------------------------------------- presets
def expression_presets() -> tuple[str, ...]:
    return EXPRESSION_PRESETS


def pose_presets() -> tuple[str, ...]:
    return POSE_PRESETS


def is_valid_expression(name: str) -> bool:
    return name in EXPRESSION_PRESETS


def is_valid_pose(name: str) -> bool:
    return name in POSE_PRESETS


# ----------------------------------------------------------------- sheet
def sheet_prompt_for_view(character: Character, view: str) -> str:
    """Build the sheet-generation prompt for a given view (spec 13.5)."""

    base = build_character_prompt(character, include_negative=False)
    if view == "front":
        return f"{base}, front view, full body, neutral expression, simple background, character sheet"
    if view == "side":
        return f"{base}, side view, full body, neutral expression, simple background, character sheet"
    if view == "back":
        return f"{base}, back view, full body, neutral expression, simple background, character sheet"
    if view == "face":
        return f"{base}, face close-up, front view, detailed eyes, simple background, character sheet"
    if view == "expression":
        return (
            f"{base}, expression sheet, multiple expressions: "
            + ", ".join(EXPRESSION_PRESETS[:8])
        )
    if view == "outfit":
        return f"{base}, outfit detail sheet, full body, labelled outfit pieces, simple background"
    raise ValueError(f"unknown sheet view: {view!r}")


__all__ = [
    "CHARACTERS_FILENAME",
    "CHARACTER_CARD_FILENAME",
    "CharacterService",
    "CharacterNotFoundError",
    "CharacterValidationError",
    "EXPRESSION_PRESETS",
    "POSE_PRESETS",
    "ReferenceUpload",
    "SHEET_VIEWS",
    "build_character_negative",
    "build_character_prompt",
    "build_ip_adapter_overrides",
    "build_lora_overrides",
    "expression_presets",
    "is_valid_expression",
    "is_valid_pose",
    "pose_presets",
    "sheet_prompt_for_view",
]
