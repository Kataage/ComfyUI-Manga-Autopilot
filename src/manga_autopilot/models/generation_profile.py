"""Generation profiles and semantic prompt segments for the Anima pipeline.

A generation profile owns every technical field of a render (steps, CFG, sampler,
scheduler, seed handling, and effective dimensions). The LLM only ever supplies
semantic segments, so profiles stay reproducible across runs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from manga_autopilot.models.character import LoraRef


class LicenseMetadata(BaseModel):
    """Licence terms a profile's weights are distributed under."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=512)
    requires_acknowledgement: bool = True
    allows_bundling: bool = False
    allows_auto_download: bool = False
    notes: str = Field(default="", max_length=1024)


class GenerationSettings(BaseModel):
    """Sampler-level settings that the application, not the LLM, decides."""

    model_config = ConfigDict(extra="forbid")

    steps: int = Field(ge=1, le=200)
    cfg: float = Field(ge=0.0, le=30.0)
    sampler: str = Field(min_length=1, max_length=64)
    scheduler: str = Field(min_length=1, max_length=64)


class ModelAssets(BaseModel):
    """File names the workflow expects to find in the local ComfyUI install."""

    model_config = ConfigDict(extra="forbid")

    unet: str = Field(default="", max_length=256)
    text_encoder: str = Field(default="", max_length=256)
    vae: str = Field(default="", max_length=256)
    loras: list[LoraRef] = Field(default_factory=list)


class ResolutionPolicy(BaseModel):
    """Deterministic mapping from a panel aspect ratio to render dimensions."""

    model_config = ConfigDict(extra="forbid")

    target_pixels: int = Field(default=960 * 1280, ge=64 * 64)
    multiple_of: int = Field(default=64, ge=1, le=256)
    min_side: int = Field(default=512, ge=64, le=4096)
    max_side: int = Field(default=1536, ge=64, le=4096)
    default_aspect_width: float = Field(default=3.0, gt=0.0)
    default_aspect_height: float = Field(default=4.0, gt=0.0)


class ResolvedResolution(BaseModel):
    """Effective render dimensions produced by a `ResolutionPolicy`."""

    model_config = ConfigDict(extra="forbid")

    width: int = Field(ge=64, le=4096)
    height: int = Field(ge=64, le=4096)

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


class GenerationProfile(BaseModel):
    """A named, reproducible render configuration."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    assets: ModelAssets = Field(default_factory=ModelAssets)
    generation: GenerationSettings
    resolution: ResolutionPolicy = Field(default_factory=ResolutionPolicy)
    candidate_count: int = Field(default=1, ge=1, le=8)
    technical_retry_count: int = Field(default=1, ge=0, le=4)
    quality_retry_count: int = Field(default=0, ge=0, le=4)
    quality_prefix: list[str] = Field(default_factory=list)
    style_defaults: list[str] = Field(default_factory=list)
    negative_defaults: list[str] = Field(default_factory=list)
    allow_score_tags: bool = True
    license: LicenseMetadata

    @property
    def is_anima(self) -> bool:
        return self.id.startswith("anima_")


class SemanticPromptSegments(BaseModel):
    """Semantic prompt material. Technical overrides are accepted but never applied."""

    model_config = ConfigDict(extra="forbid")

    identity: list[str] = Field(default_factory=list)
    must_keep: list[str] = Field(default_factory=list)
    subject: list[str] = Field(default_factory=list)
    action: list[str] = Field(default_factory=list)
    camera: list[str] = Field(default_factory=list)
    emotion: list[str] = Field(default_factory=list)
    background: list[str] = Field(default_factory=list)
    lighting: list[str] = Field(default_factory=list)
    style: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)
    technical_overrides: dict[str, Any] = Field(default_factory=dict)


#: Positive-prompt segment order. Identity and continuity material comes first so
#: that truncation by a text encoder drops decoration, never character identity.
POSITIVE_SEGMENT_ORDER: tuple[str, ...] = (
    "identity",
    "must_keep",
    "subject",
    "action",
    "camera",
    "emotion",
    "background",
    "lighting",
    "style",
)

#: Fields a profile owns outright. Values supplied by an LLM are discarded.
PROFILE_OWNED_FIELDS: frozenset[str] = frozenset(
    {"seed", "width", "height", "steps", "cfg", "sampler", "scheduler"}
)


__all__ = [
    "POSITIVE_SEGMENT_ORDER",
    "PROFILE_OWNED_FIELDS",
    "GenerationProfile",
    "GenerationSettings",
    "LicenseMetadata",
    "ModelAssets",
    "ResolutionPolicy",
    "ResolvedResolution",
    "SemanticPromptSegments",
]
