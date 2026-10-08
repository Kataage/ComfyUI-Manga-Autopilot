"""Bounded memory/disk budgets for persisted Work Page PNG rendering.

Only named, server-owned profiles may select limits. Browser dimensions and
untrusted Work geometry cannot raise these ceilings. Limit source pixels as
well as the output canvas, since Pillow decodes entire candidate images.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PagePngBudget:
    name: str
    max_pixels: int
    max_input_pixels: int
    max_total_input_pixels: int
    max_png_bytes: int
    max_input_bytes: int
    max_total_input_bytes: int


PROFILES: dict[str, PagePngBudget] = {
    # Allow common screen/comic spreads; roughly 48 MiB RGB / 64 MiB RGBA.
    "screen": PagePngBudget(
        "screen", 12_000_000, 16_000_000, 32_000_000,
        48 * 1024 * 1024, 48 * 1024 * 1024, 96 * 1024 * 1024,
    ),
    # Opt-in print layout (e.g. 6000 x 4000); roughly 96 MiB RGB.
    "print": PagePngBudget(
        "print", 24_000_000, 24_000_000, 48_000_000,
        96 * 1024 * 1024, 96 * 1024 * 1024, 192 * 1024 * 1024,
    ),
}
DEFAULT_PROFILE = "screen"
MAX_SERVABLE_PNG_BYTES = max(p.max_png_bytes for p in PROFILES.values())
PNG_IO_CHUNK_BYTES = 256 * 1024
PNG_SPOOL_MEMORY_BYTES = 1024 * 1024


def resolve_page_png_budget(profile: str) -> PagePngBudget:
    """Never accept arbitrary client limits or an undocumented profile."""
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError(
            f"export_profile must be one of {', '.join(sorted(PROFILES))}"
        )
    return PROFILES[profile]


__all__ = [
    "DEFAULT_PROFILE", "MAX_SERVABLE_PNG_BYTES", "PNG_IO_CHUNK_BYTES",
    "PNG_SPOOL_MEMORY_BYTES", "PROFILES", "PagePngBudget", "resolve_page_png_budget",
]
