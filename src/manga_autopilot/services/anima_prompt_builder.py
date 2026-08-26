"""Deterministic prompt rendering for Anima generation profiles.

Unlike `PromptBuilder`, this adapter makes no LLM call. It takes semantic segments
produced upstream and renders them into a `PromptSpec` whose technical fields come
solely from the profile, so a rerun of the same panel is byte-identical.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from manga_autopilot.models.generation_profile import (
    PROFILE_OWNED_FIELDS,
    GenerationProfile,
    SemanticPromptSegments,
)
from manga_autopilot.services.generation_profiles import (
    resolve_default_resolution,
    resolve_panel_resolution,
)
from manga_autopilot.services.prompt_builder import PromptSpec

log = logging.getLogger(__name__)

SCORE_TAG_PATTERN = re.compile(r"^score_\S*$", re.IGNORECASE)


def _split_terms(values: Iterable[str]) -> list[str]:
    terms: list[str] = []
    for value in values:
        for part in str(value).split(","):
            term = part.strip()
            if term:
                terms.append(term)
    return terms


def _is_score_tag(term: str) -> bool:
    return bool(SCORE_TAG_PATTERN.match(term))


def _join(terms: Sequence[str]) -> str:
    return ", ".join(terms)


@dataclass
class AnimaPromptBuilder:
    """Render semantic segments into a profile-owned `PromptSpec`."""

    separator: str = ", "

    def render(
        self,
        segments: SemanticPromptSegments,
        profile: GenerationProfile,
        *,
        seed: int,
        panel_size: tuple[float, float] | None = None,
    ) -> PromptSpec:
        """Return the prompt spec for one panel.

        `segments.technical_overrides` is accepted for forward compatibility with
        looser upstream planners but never applied: steps, CFG, seed, dimensions,
        sampler, and scheduler always come from `profile` and `seed`.
        """
        self._warn_on_overrides(segments, profile)

        identity = self._normalise(segments.identity, profile)
        must_keep = self._normalise(segments.must_keep, profile)
        subject = self._normalise(segments.subject, profile)
        action = self._normalise(segments.action, profile)
        camera = self._normalise(segments.camera, profile)
        emotion = self._normalise(segments.emotion, profile)
        background = self._normalise(segments.background, profile)
        lighting = self._normalise(segments.lighting, profile)
        style = self._normalise(
            [*segments.style, *profile.style_defaults],
            profile,
        )
        quality = self._normalise(profile.quality_prefix, profile)

        positive = self._dedupe(
            [
                *quality,
                *identity,
                *must_keep,
                *subject,
                *action,
                *camera,
                *emotion,
                *background,
                *lighting,
                *style,
            ]
        )
        negative = self._dedupe(
            self._normalise([*profile.negative_defaults, *segments.negative], profile)
        )

        if panel_size is None:
            resolution = resolve_default_resolution(profile.resolution)
        else:
            resolution = resolve_panel_resolution(*panel_size, profile.resolution)

        return PromptSpec(
            positive=_join(positive),
            negative=_join(negative),
            character_prompt=_join(self._dedupe([*identity, *must_keep])),
            background_prompt=_join(background),
            action_prompt=_join(action),
            camera_prompt=_join(camera),
            emotion_prompt=_join(emotion),
            style_prompt=_join(style),
            quality_prompt=_join(quality),
            seed=int(seed),
            width=resolution.width,
            height=resolution.height,
            steps=profile.generation.steps,
            cfg=profile.generation.cfg,
            sampler=profile.generation.sampler,
            scheduler=profile.generation.scheduler,
        )

    def _normalise(self, values: Iterable[str], profile: GenerationProfile) -> list[str]:
        terms = _split_terms(values)
        if profile.allow_score_tags:
            return terms
        return [term for term in terms if not _is_score_tag(term)]

    def _dedupe(self, terms: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for term in terms:
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(term)
        return ordered

    def _warn_on_overrides(
        self, segments: SemanticPromptSegments, profile: GenerationProfile
    ) -> None:
        ignored = sorted(PROFILE_OWNED_FIELDS.intersection(segments.technical_overrides))
        if ignored:
            log.warning(
                "ignoring technical overrides %s for profile %s; the profile owns these fields",
                ignored,
                profile.id,
            )


__all__ = ["AnimaPromptBuilder", "SCORE_TAG_PATTERN"]
