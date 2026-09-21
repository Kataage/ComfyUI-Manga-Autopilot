"""Built-in generation profile loading and deterministic resolution resolving."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

from manga_autopilot.models.generation_profile import (
    GenerationProfile,
    ResolutionPolicy,
    ResolvedResolution,
)

PROFILE_PACKAGE = "manga_autopilot.profiles"

#: Profile IDs shipped with the extension, in menu order.
BUILTIN_PROFILE_IDS: tuple[str, ...] = ("anima_base", "anima_aesthetic", "anima_turbo")


@cache
def _load_profile_cached(profile_id: str) -> GenerationProfile:
    resource = resources.files(PROFILE_PACKAGE).joinpath(f"{profile_id}.json")
    if not resource.is_file():
        raise KeyError(f"unknown generation profile: {profile_id}")
    data = json.loads(resource.read_text(encoding="utf-8"))
    profile = GenerationProfile.model_validate(data)
    if profile.id != profile_id:
        raise ValueError(
            f"generation profile file {profile_id}.json declares id {profile.id!r}"
        )
    return profile


def load_builtin_profile(profile_id: str) -> GenerationProfile:
    """Return the packaged profile named `profile_id`.

    Raises:
        KeyError: the profile is not one of the packaged built-ins.
    """
    if profile_id not in BUILTIN_PROFILE_IDS:
        raise KeyError(f"unknown generation profile: {profile_id}")
    return _load_profile_cached(profile_id)


def list_builtin_profiles() -> list[GenerationProfile]:
    """Return every packaged profile in menu order."""
    return [load_builtin_profile(profile_id) for profile_id in BUILTIN_PROFILE_IDS]


def _round_to_multiple(value: float, multiple: int) -> int:
    return max(multiple, int(round(value / multiple)) * multiple)


def _clamp_to_multiple(value: int, policy: ResolutionPolicy) -> int:
    lowest = _round_to_multiple(policy.min_side, policy.multiple_of)
    highest = (policy.max_side // policy.multiple_of) * policy.multiple_of
    return min(max(value, lowest), highest)


def resolve_panel_resolution(
    width: float,
    height: float,
    policy: ResolutionPolicy,
) -> ResolvedResolution:
    """Map a panel's aspect ratio onto render dimensions.

    The panel's absolute size is irrelevant; only its ratio matters. Dimensions
    keep the policy's pixel budget, snap to `multiple_of`, and stay inside the
    model's supported side range, so the same panel always renders at the same
    size.

    Raises:
        ValueError: either panel dimension is not positive.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"panel width and height must be positive, got {width}x{height}")

    aspect = width / height
    ideal_height = (policy.target_pixels / aspect) ** 0.5
    ideal_width = ideal_height * aspect

    resolved_width = _clamp_to_multiple(
        _round_to_multiple(ideal_width, policy.multiple_of), policy
    )
    resolved_height = _clamp_to_multiple(
        _round_to_multiple(ideal_height, policy.multiple_of), policy
    )
    return ResolvedResolution(width=resolved_width, height=resolved_height)


def resolve_default_resolution(policy: ResolutionPolicy) -> ResolvedResolution:
    """Resolve the policy's own default aspect ratio."""
    return resolve_panel_resolution(
        policy.default_aspect_width, policy.default_aspect_height, policy
    )


__all__ = [
    "BUILTIN_PROFILE_IDS",
    "PROFILE_PACKAGE",
    "list_builtin_profiles",
    "load_builtin_profile",
    "resolve_default_resolution",
    "resolve_panel_resolution",
]
