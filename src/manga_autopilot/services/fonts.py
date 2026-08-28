"""Resolve a font family name to a real TrueType file.

Bubble text carries a `FontSpec` - family, size, weight - all the way from
planning to rendering, and the renderer used to hand none of it to Pillow.
`ImageDraw.text` without a `font=` argument falls back to a built-in bitmap
face that has no CJK glyphs and ignores the requested size, so Japanese
dialogue rendered as nothing at all inside a correctly drawn bubble.

Resolution order, first hit wins:

1. An explicit directory the caller supplies (a project may ship its own fonts).
2. The platform font directories, matched against the family name.
3. A known CJK-capable fallback, so Japanese still renders.

If nothing matches, the caller gets Pillow's default and a warning that says
what will be missing, rather than a blank bubble and silence.
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

log = logging.getLogger(__name__)

#: Where each platform keeps its fonts.
SYSTEM_FONT_DIRS: tuple[Path, ...] = (
    Path(r"C:\Windows\Fonts"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
)

FONT_SUFFIXES: tuple[str, ...] = (".ttf", ".ttc", ".otf")

#: Families that carry Japanese glyphs, best first. Used when the requested
#: family is not installed - a wrong-but-legible face beats an empty bubble.
CJK_FALLBACK_FAMILIES: tuple[str, ...] = (
    "NotoSansJP",
    "NotoSansCJK",
    "YuGothM",
    "YuGothR",
    "meiryo",
    "msgothic",
    "BIZ-UDGothicR",
    "Hiragino",
    "AppleGothic",
)


def _normalise(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _iter_font_files(extra_dir: Path | None = None):
    directories = ([extra_dir] if extra_dir else []) + list(SYSTEM_FONT_DIRS)
    for directory in directories:
        if not directory or not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in FONT_SUFFIXES:
                yield path


def find_font_file(family: str, extra_dir: Path | str | None = None) -> Path | None:
    """Return the font file whose name best matches `family`, if any."""
    if not family:
        return None
    wanted = _normalise(family)
    extra = Path(extra_dir) if extra_dir else None

    exact: Path | None = None
    partial: Path | None = None
    for path in _iter_font_files(extra):
        stem = _normalise(path.stem)
        if stem == wanted:
            exact = path
            break
        if partial is None and wanted in stem:
            partial = path
    return exact or partial


@lru_cache(maxsize=64)
def _cached_font(path_str: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path_str, size)


def load_font(
    family: str,
    size: float,
    *,
    extra_dir: Path | str | None = None,
    require_cjk: bool = True,
) -> ImageFont.ImageFont:
    """Load `family` at `size`, falling back to a CJK-capable face.

    `require_cjk` keeps Japanese legible when the requested family is missing.
    Set it to False for text that is known to be Latin-only.
    """
    points = max(1, int(round(size)))

    path = find_font_file(family, extra_dir)
    if path is None and require_cjk:
        for fallback in CJK_FALLBACK_FAMILIES:
            path = find_font_file(fallback, extra_dir)
            if path is not None:
                log.info(
                    "font %r is not installed; falling back to %s", family, path.name
                )
                break

    if path is None:
        log.warning(
            "no usable font found for %r on %s; bubble text will render with "
            "Pillow's built-in face, which has no CJK glyphs",
            family,
            sys.platform,
        )
        return ImageFont.load_default()

    try:
        return _cached_font(str(path), points)
    except OSError as exc:
        log.warning("could not load %s: %s; using the built-in face", path, exc)
        return ImageFont.load_default()


def has_cjk_font(extra_dir: Path | str | None = None) -> bool:
    """Whether any CJK-capable font is available to render Japanese."""
    return any(find_font_file(name, extra_dir) for name in CJK_FALLBACK_FAMILIES)


__all__ = [
    "CJK_FALLBACK_FAMILIES",
    "FONT_SUFFIXES",
    "SYSTEM_FONT_DIRS",
    "find_font_file",
    "has_cjk_font",
    "load_font",
]
