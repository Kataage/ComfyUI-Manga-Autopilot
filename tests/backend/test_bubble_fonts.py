"""Bubble text has to actually appear (plan follow-up, 2026-08-28).

A completed live run produced pages whose speech bubbles were drawn correctly
and were entirely empty: `ImageDraw.text` was called with no `font=`, so Pillow
used its built-in bitmap face, which has no CJK glyphs and ignores the requested
size. The FontSpec was modelled, persisted, and never handed to the renderer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from manga_autopilot.models.bubble import FontSpec, SpeechBubble
from manga_autopilot.services.bubble_renderer import draw_bubble_on_canvas
from manga_autopilot.services.fonts import (
    CJK_FALLBACK_FAMILIES,
    find_font_file,
    has_cjk_font,
    load_font,
)

requires_cjk_font = pytest.mark.skipif(
    not has_cjk_font(), reason="no CJK-capable font is installed"
)


def _bubble(text: str, direction: str, size: float = 22.0) -> SpeechBubble:
    return SpeechBubble(
        id="b0",
        panel_id="p1",
        type="normal",
        text=text,
        x=0,
        y=0,
        width=220,
        height=200,
        font=FontSpec(family="NotoSansJP", size=size, color="#000000"),
        direction=direction,
        order=0,
    )


def _ink(canvas: Image.Image, box: tuple[int, int, int, int] | None = None) -> int:
    """Count near-black pixels, i.e. drawn text."""
    region = canvas.crop(box) if box else canvas
    return sum(1 for value in region.convert("L").tobytes() if value < 90)


# ------------------------------------------------------------------ resolution


def test_a_missing_family_falls_back_to_something_that_has_glyphs() -> None:
    assert find_font_file("NoSuchFontAnywhere") is None

    face = load_font("NoSuchFontAnywhere", 20)

    # Either a real fallback face, or Pillow's default on a machine with none.
    assert face is not None


@requires_cjk_font
def test_the_fallback_is_a_real_face_at_the_requested_size() -> None:
    face = load_font("NoSuchFontAnywhere", 26)

    assert getattr(face, "size", None) == 26
    assert getattr(face, "path", "")


def test_an_empty_family_resolves_to_nothing() -> None:
    assert find_font_file("") is None


def test_the_fallback_list_is_ordered_and_non_empty() -> None:
    assert CJK_FALLBACK_FAMILIES
    assert "NotoSansJP" in CJK_FALLBACK_FAMILIES


def test_a_project_font_directory_wins(tmp_path: Path) -> None:
    """A project may ship its own face; that takes precedence over the system."""
    shipped = tmp_path / "MangaFont.ttf"
    shipped.write_bytes(b"not really a font")

    assert find_font_file("MangaFont", tmp_path) == shipped


def test_an_unloadable_file_degrades_instead_of_raising(tmp_path: Path) -> None:
    broken = tmp_path / "Broken.ttf"
    broken.write_bytes(b"not really a font")

    face = load_font("Broken", 18, extra_dir=tmp_path, require_cjk=False)

    assert face is not None  # Pillow's default, not an exception


# --------------------------------------------------------------------- drawing


@requires_cjk_font
def test_japanese_text_actually_draws_ink() -> None:
    canvas = Image.new("RGB", (260, 240), (255, 255, 255))

    draw_bubble_on_canvas(canvas, _bubble("最後の配達です", "horizontal"), 20, 20, 220, 200)

    # The bubble outline alone is thin; glyphs add substantially more ink.
    assert _ink(canvas) > 200


@requires_cjk_font
def test_an_empty_bubble_draws_no_text_ink() -> None:
    with_text = Image.new("RGB", (260, 240), (255, 255, 255))
    without = Image.new("RGB", (260, 240), (255, 255, 255))

    draw_bubble_on_canvas(with_text, _bubble("最後の配達です", "horizontal"), 20, 20, 220, 200)
    draw_bubble_on_canvas(without, _bubble("", "horizontal"), 20, 20, 220, 200)

    assert _ink(with_text) > _ink(without) + 200


@requires_cjk_font
def test_vertical_text_stays_inside_the_bubble() -> None:
    """The column used to sit on the bounding box edge, outside the ellipse.

    Comparing against the same bubble with no text isolates the glyphs from the
    outline, which sweeps through any corner region an absolute box would use.
    """
    with_text = Image.new("RGB", (260, 240), (255, 255, 255))
    outline_only = Image.new("RGB", (260, 240), (255, 255, 255))

    draw_bubble_on_canvas(
        with_text, _bubble("雨の音だけが街を包む", "vertical"), 20, 20, 220, 200
    )
    draw_bubble_on_canvas(outline_only, _bubble("", "vertical"), 20, 20, 220, 200)

    # Top-right corner: inside the bounding box, outside the ellipse. This is
    # exactly where the old top-aligned, box-edge column put its first glyphs.
    corner = (196, 20, 240, 56)
    assert _ink(with_text, corner) == _ink(outline_only, corner)
    # ...while the bubble as a whole clearly gained glyphs.
    assert _ink(with_text) > _ink(outline_only) + 200


@requires_cjk_font
def test_vertical_text_is_centred_rather_than_top_aligned() -> None:
    canvas = Image.new("RGB", (260, 240), (255, 255, 255))

    draw_bubble_on_canvas(canvas, _bubble("行くぞ", "vertical"), 20, 20, 220, 200)

    top_band = _ink(canvas, (20, 20, 240, 60))
    middle_band = _ink(canvas, (20, 80, 240, 160))
    assert middle_band > top_band


@requires_cjk_font
def test_a_larger_font_draws_more_ink() -> None:
    """The requested size used to be ignored entirely."""
    small = Image.new("RGB", (260, 240), (255, 255, 255))
    large = Image.new("RGB", (260, 240), (255, 255, 255))

    draw_bubble_on_canvas(small, _bubble("配達", "horizontal", size=12), 20, 20, 220, 200)
    draw_bubble_on_canvas(large, _bubble("配達", "horizontal", size=40), 20, 20, 220, 200)

    assert _ink(large) > _ink(small)


# ----------------------------------------------------------- fitting the box
#
# The lettering hook gives every bubble the same 160x80 box regardless of how
# much dialogue it holds, so a live page rendered 「行くぞ」 as 「行く」.


def test_vertical_size_shrinks_for_longer_text() -> None:
    from manga_autopilot.services.bubble_renderer import _fit_vertical_size

    short = _fit_vertical_size("行くぞ", 80, 18.0, 1.4)
    long = _fit_vertical_size("……やっと、届いたのね。", 80, 18.0, 1.4)

    assert long < short <= 18.0


def test_vertical_size_never_grows_past_the_request() -> None:
    from manga_autopilot.services.bubble_renderer import _fit_vertical_size

    assert _fit_vertical_size("行", 400, 18.0, 1.4) == 18.0


def test_vertical_size_has_a_readable_floor() -> None:
    from manga_autopilot.services.bubble_renderer import MIN_FONT_SIZE, _fit_vertical_size

    assert _fit_vertical_size("あ" * 200, 80, 18.0, 1.4) == MIN_FONT_SIZE


@requires_cjk_font
def test_every_character_is_drawn_when_the_bubble_is_small() -> None:
    """Three characters in a box sized for two must still all appear."""
    small = Image.new("RGB", (200, 120), (255, 255, 255))
    two_chars = Image.new("RGB", (200, 120), (255, 255, 255))

    three = _bubble("行くぞ", "vertical", size=18)
    two = _bubble("行く", "vertical", size=18)
    draw_bubble_on_canvas(small, three, 10, 10, 160, 80)
    draw_bubble_on_canvas(two_chars, two, 10, 10, 160, 80)

    # A third glyph means more ink than the same bubble holding two.
    assert _ink(small) > _ink(two_chars)


@requires_cjk_font
def test_horizontal_text_shrinks_rather_than_overflowing() -> None:
    narrow = Image.new("RGB", (240, 140), (255, 255, 255))

    draw_bubble_on_canvas(
        narrow, _bubble("最後の配達をお届けにあがりました", "horizontal", size=28), 10, 10, 200, 100
    )

    # Nothing may be drawn outside the bubble's own box.
    assert _ink(narrow, (212, 0, 240, 140)) == 0
