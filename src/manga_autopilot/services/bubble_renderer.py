"""Speech bubble shape rendering (spec section 19.4).

Supports ``normal`` (ellipse with tail) and ``shout`` (jagged polygon) shapes.
Thought, narration, whisper, and radio shapes reuse the ellipse renderer with
different stroke styling — they will be specialised in later issues.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from manga_autopilot.models.bubble import FontSpec, SpeechBubble, TailTarget
from manga_autopilot.services.fonts import load_font


@dataclass
class BubbleRenderResult:
    bubble_id: str
    output_path: str
    width: int
    height: int


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _ellipse_points(
    x: float, y: float, w: float, h: float, segments: int = 64
) -> list[tuple[float, float]]:
    cx, cy = x + w / 2, y + h / 2
    rx, ry = w / 2, h / 2
    pts: list[tuple[float, float]] = []
    for i in range(segments):
        theta = (2 * math.pi * i) / segments
        pts.append((cx + rx * math.cos(theta), cy + ry * math.sin(theta)))
    return pts


def _cloud_points(
    x: float, y: float, w: float, h: float, lobes: int = 9
) -> list[tuple[float, float]]:
    """Return a cloud-shaped point list made of overlapping circles.

    Each lobe is a small circle on the ellipse perimeter.  Drawing the
    polygon as a single ``PIL.ImageDraw.polygon`` gives a cloud-like
    silhouette with a single outline.
    """

    cx, cy = x + w / 2, y + h / 2
    rx, ry = w / 2, h / 2
    lobe_radius = min(w, h) / (2 * lobes)
    pts: list[tuple[float, float]] = []
    for i in range(lobes):
        theta = (2 * math.pi * i) / lobes
        lx = cx + (rx - lobe_radius) * math.cos(theta)
        ly = cy + (ry - lobe_radius) * math.sin(theta)
        for k in range(16):
            phi = (2 * math.pi * k) / 16
            pts.append((lx + lobe_radius * math.cos(phi), ly + lobe_radius * math.sin(phi)))
    return pts


def _jagged_points(
    x: float, y: float, w: float, h: float, spikes: int = 18, jitter: float = 0.12
) -> list[tuple[float, float]]:
    """Return points along a jagged ellipse for shout bubbles."""

    cx, cy = x + w / 2, y + h / 2
    rx, ry = w / 2, h / 2
    pts: list[tuple[float, float]] = []
    for i in range(spikes * 2):
        theta = (2 * math.pi * i) / (spikes * 2)
        r_jitter = 1.0 + (jitter if i % 2 == 0 else -jitter / 2)
        pts.append(
            (
                cx + rx * r_jitter * math.cos(theta),
                cy + ry * r_jitter * math.sin(theta),
            )
        )
    return pts


def _text_anchor(
    direction: str, x: float, y: float, w: float, h: float
) -> str:
    return "la" if direction == "vertical" else "mm"


#: Never shrink text below this; past it the line is unreadable anyway and
#: truncation is the more honest outcome.
MIN_FONT_SIZE = 8.0


def _fit_vertical_size(text: str, height: float, requested: float, line_ratio: float) -> float:
    """Largest size <= `requested` that fits every character in one column."""
    if not text:
        return requested
    usable = max(1.0, height - 8)
    ideal = usable / (len(text) * max(0.1, line_ratio))
    return max(MIN_FONT_SIZE, min(requested, ideal))


def _fit_horizontal_size(
    draw: ImageDraw.ImageDraw,
    text: str,
    width: float,
    requested: float,
    family: str,
    font_dir: Path | str | None,
) -> tuple[float, object]:
    """Largest size <= `requested` whose rendered width fits, with its face."""
    usable = max(1.0, width - 8)
    size = requested
    while size > MIN_FONT_SIZE:
        face = load_font(family, size, extra_dir=font_dir)
        try:
            measured = draw.textlength(text, font=face)
        except (AttributeError, TypeError):  # pragma: no cover - very old Pillow
            return size, face
        if measured <= usable:
            return size, face
        size -= 1
    return MIN_FONT_SIZE, load_font(family, MIN_FONT_SIZE, extra_dir=font_dir)


def _render_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: FontSpec,
    x: float,
    y: float,
    w: float,
    h: float,
    direction: str,
    font_dir: Path | str | None = None,
) -> None:
    if not text:
        return
    color = _hex_to_rgb(font.color)
    # Without a real face Pillow uses a bitmap font with no CJK glyphs and a
    # fixed size, which drew Japanese dialogue as nothing inside a perfectly
    # good bubble.
    face = load_font(font.family, font.size, extra_dir=font_dir)
    if direction == "vertical":
        # A column down the right side, kept inside the ellipse rather than the
        # bounding box: at the top and bottom of the column the ellipse is much
        # narrower than the box, which is where text used to spill outside.
        # Shrink to fit rather than cutting the line off: a bubble sized for
        # two characters used to render 「行くぞ」 as 「行く」.
        size = _fit_vertical_size(text, h, font.size, font.line_height)
        if size != font.size:
            face = load_font(font.family, size, extra_dir=font_dir)
        line_height = max(8, int(size * font.line_height))
        fits = max(1, int((h - 8) // line_height))
        shown = text[:fits]
        block = len(shown) * line_height

        centre_x = x + w / 2
        centre_y = y + h / 2
        semi_x, semi_y = w / 2, h / 2
        extreme = min(block / 2, semi_y)
        # Half-width of the ellipse at the column's furthest point from centre.
        narrow = semi_x * math.sqrt(max(0.0, 1.0 - (extreme / semi_y) ** 2))
        char_x = int(centre_x + max(0.0, narrow - size))

        cy = centre_y - block / 2
        for ch in shown:
            # Rotating the glyph itself still needs a separate pass; the
            # classifier stays exposed so the bubble service can do that.
            draw.text((char_x, int(cy)), ch, fill=color, font=face)
            cy += line_height
    else:
        size, face = _fit_horizontal_size(draw, text, w, font.size, font.family, font_dir)
        anchor = _text_anchor(direction, x, y, w, h)
        try:
            draw.text(
                (int(x + w / 2), int(y + h / 2)),
                text,
                fill=color,
                anchor=anchor,
                font=face,
            )
        except TypeError:
            draw.text(
                (int(x + 4), int(y + h / 2 - size / 2)),
                text,
                fill=color,
                font=face,
            )


# Japanese vertical punctuation that traditionally rotates 90deg clockwise.
_VERTICAL_ROTATE_CHARS = set("、。,.「」『』?!！？・…ー")


def _should_rotate_vertical_punctuation(char: str) -> bool:
    """Return True for punctuation that should rotate in vertical Japanese."""

    return char in _VERTICAL_ROTATE_CHARS


def _draw_tail(
    draw: ImageDraw.ImageDraw,
    shape_points: list[tuple[float, float]],
    target: TailTarget | None,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int],
    width: int,
) -> None:
    if target is None or len(shape_points) < 8:
        return
    # Pick the point closest to the target as the tail base.
    base = min(shape_points, key=lambda p: (p[0] - target.x) ** 2 + (p[1] - target.y) ** 2)
    base_idx = shape_points.index(base)
    prev = shape_points[(base_idx - 1) % len(shape_points)]
    nxt = shape_points[(base_idx + 1) % len(shape_points)]
    draw.polygon([prev, (target.x, target.y), nxt], fill=fill, outline=outline)
    if width > 0:
        draw.line([prev, (target.x, target.y), nxt], fill=outline, width=width)


def draw_bubble_on_canvas(
    canvas: Image.Image,
    bubble: SpeechBubble,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    """Render a bubble directly onto an existing :class:`PIL.Image`."""

    draw = ImageDraw.Draw(canvas)
    fill = (255, 255, 255)
    outline = (0, 0, 0)
    stroke = 3
    if bubble.type == "shout":
        points = _jagged_points(x, y, width, height)
        draw.polygon(points, fill=fill, outline=outline)
        _draw_tail(draw, points, bubble.tail_target, fill, outline, stroke)
    elif bubble.type == "thought":
        points = _cloud_points(x, y, width, height)
        draw.polygon(points, fill=fill, outline=outline)
        # Two small leading circles act as a "thought" tail.
        if bubble.tail_target is not None:
            tx, ty = bubble.tail_target.x, bubble.tail_target.y
            for radius in (10, 5):
                d = ((tx - x) ** 2 + (ty - y) ** 2) ** 0.5
                if d == 0:
                    break
                ux = (tx - x) / d
                uy = (ty - y) / d
                cxp = tx + ux * radius
                cyp = ty + uy * radius
                draw.ellipse(
                    [
                        (cxp - radius, cyp - radius),
                        (cxp + radius, cyp + radius),
                    ],
                    fill=fill,
                    outline=outline,
                )
    else:
        points = _ellipse_points(x, y, width, height)
        draw.polygon(points, fill=fill, outline=outline)
        _draw_tail(draw, points, bubble.tail_target, fill, outline, stroke)
    _render_text(draw, bubble.text, bubble.font, x, y, width, height, bubble.direction)


def render_bubble_to_png(
    bubble: SpeechBubble,
    output_path: str,
    *,
    width: int = 480,
    height: int = 240,
) -> BubbleRenderResult:
    """Render a single bubble to its own PNG (debug / preview)."""

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw_bubble_on_canvas(image, bubble, 20, 20, width - 40, height - 40)
    image.save(output_path, format="PNG")
    return BubbleRenderResult(
        bubble_id=bubble.id, output_path=output_path, width=width, height=height
    )


__all__ = [
    "BubbleRenderResult",
    "draw_bubble_on_canvas",
    "render_bubble_to_png",
]
