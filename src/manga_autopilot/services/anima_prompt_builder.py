"""Deterministic prompt rendering for Anima generation profiles.

Unlike `PromptBuilder`, this adapter makes no LLM call. It takes semantic segments
produced upstream and renders them into a `PromptSpec` whose technical fields come
solely from the profile, so a rerun of the same panel is byte-identical.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from manga_autopilot.models.generation_profile import (
    PROFILE_OWNED_FIELDS,
    GenerationProfile,
    SemanticPromptSegments,
)
from manga_autopilot.models.page import PanelPlan
from manga_autopilot.models.panel import PanelRecord
from manga_autopilot.services.generation_profiles import (
    resolve_default_resolution,
    resolve_panel_resolution,
)
from manga_autopilot.services.prompt_builder import PromptSpec

log = logging.getLogger(__name__)

SCORE_TAG_PATTERN = re.compile(r"^score_\S*$", re.IGNORECASE)

#: Phrasings that try to remove something by naming it.
#:
#: Diffusion models render the noun regardless of the grammar around it, and at
#: CFG 1 there is no negative branch to fall back on either. Observed on Anima:
#: a scene reading "the bikini has been fully removed" rendered a bikini in all
#: 28 scenes, and deleting the noun outright was the only thing that fixed it.
#: The fix is always to describe the wanted state without naming the unwanted
#: thing - "her bare shoulders are visible", not "no longer wearing the dress".
NEGATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bno\s+\w+", re.IGNORECASE),
    re.compile(r"\bnot\s+\w+", re.IGNORECASE),
    re.compile(r"\bwithout\s+\w+", re.IGNORECASE),
    re.compile(r"\bno\s+longer\b", re.IGNORECASE),
    re.compile(r"\b\w+\s+(?:has|have)\s+been\s+removed\b", re.IGNORECASE),
    re.compile(r"\bremoved\b", re.IGNORECASE),
    re.compile(r"\bfree\s+of\s+\w+", re.IGNORECASE),
    re.compile(r"\babsent\b", re.IGNORECASE),
    re.compile(r"\blacking\s+\w+", re.IGNORECASE),
)


def find_negations(text: str) -> list[str]:
    """Return the negation phrasings in `text`, in order, without duplicates.

    Word boundaries keep ordinary vocabulary out of the results: ``snow``,
    ``notebook``, and ``nostalgic`` are not findings.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pattern in NEGATION_PATTERNS:
        for match in pattern.finditer(text):
            phrase = match.group(0).strip()
            key = phrase.casefold()
            if key not in seen:
                seen.add(key)
                found.append(phrase)
    return found


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
    reject_negations: bool = False
    """Raise instead of warning when the positive prompt tries to negate something."""

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

        positive_text = _join(positive)
        self._check_negations(positive_text, profile)

        if panel_size is None:
            resolution = resolve_default_resolution(profile.resolution)
        else:
            resolution = resolve_panel_resolution(*panel_size, profile.resolution)

        return PromptSpec(
            positive=positive_text,
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

    def _check_negations(self, positive: str, profile: GenerationProfile) -> None:
        """Flag phrasing that will render the very thing it tries to remove.

        Raises:
            ValueError: `reject_negations` is set and the prompt contains one.
        """
        found = find_negations(positive)
        if not found:
            return
        detail = ", ".join(repr(item) for item in found)
        message = (
            f"positive prompt for profile {profile.id} contains negation phrasing "
            f"({detail}); the named subject will be rendered anyway - describe the "
            "wanted state without naming the unwanted thing"
        )
        if self.reject_negations:
            raise ValueError(message)
        log.warning("%s", message)

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


def stable_panel_seed(project_id: str, panel_id: str) -> int:
    """Return a seed that depends only on which panel of which project this is.

    Regenerating the same panel therefore reproduces the same image, which is
    what makes a run snapshot worth keeping. Callers that want a different draw
    pass their own seed.
    """
    digest = hashlib.sha256(f"{project_id}/{panel_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31 - 1) + 1


def segments_from_panel_plan(
    plan: PanelPlan,
    characters: Mapping[str, Any] | None = None,
) -> SemanticPromptSegments:
    """Turn a planned panel into semantic segments for `AnimaPromptBuilder`.

    Character identity goes into `identity` and `must_keep` so it survives
    truncation by the text encoder. Nothing technical is read from the plan:
    steps, CFG, sampler, scheduler, and the dimensions belong to the profile.
    """
    records = characters or {}
    identity: list[str] = []
    must_keep: list[str] = []
    if records and not plan.characters:
        # A panel that names no one renders with no appearance anchor at all,
        # which is how a live run produced nine panels whose hair and eye colour
        # drifted freely. The planner is asked for these ids; when it returns
        # none, say so rather than rendering a stranger.
        log.warning(
            "panel %s names no character; %d are defined, so its appearance is "
            "unanchored and will drift",
            plan.panel_number,
            len(records),
        )
    for character_id in plan.characters:
        record = records.get(character_id)
        if record is None:
            # An unknown id still names someone; keep it rather than dropping
            # the only continuity signal the plan gave us.
            identity.append(character_id)
            continue
        if getattr(record, "consistency_prompt", ""):
            identity.append(record.consistency_prompt)
        must_keep.extend(record.must_keep_combined())

    camera = [value for value in (plan.shot, plan.camera_angle) if value]
    return SemanticPromptSegments(
        identity=identity,
        must_keep=must_keep,
        subject=[value for value in (plan.purpose,) if value],
        action=[plan.action] if plan.action else [],
        camera=camera,
        emotion=[plan.emotion] if plan.emotion else [],
        background=[plan.background] if plan.background else [],
    )


def panel_aspect(record: PanelRecord, profile: GenerationProfile) -> tuple[float, float]:
    """Return the panel's aspect, falling back to the profile's default."""
    layout = record.layout
    if layout is not None and layout.width > 0 and layout.height > 0:
        return (layout.width, layout.height)
    policy = profile.resolution
    return (policy.default_aspect_width, policy.default_aspect_height)


__all__ = [
    "NEGATION_PATTERNS",
    "SCORE_TAG_PATTERN",
    "AnimaPromptBuilder",
    "find_negations",
    "panel_aspect",
    "segments_from_panel_plan",
    "stable_panel_seed",
]
