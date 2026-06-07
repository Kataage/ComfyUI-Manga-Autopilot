"""Speech bubble shape rendering (spec section 19.4).

Supports ``normal`` (ellipse with tail) and ``shout`` (jagged polygon) shapes.
Thought, narration, whisper, and radio shapes reuse the ellipse renderer with
different stroke styling — they will be specialised in later issues.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageDraw

from manga_autopilot.models.bubble import FontSpec, SpeechBubble, TailTarget


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


def _render_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: FontSpec,
    x: float,
    y: float,
    w: float,
    h: float,
    direction: str,
) -> None:
    if not text:
        return
    color = _hex_to_rgb(font.color)
    # Without a font file we use the bitmap default; size is informational.
    if direction == "vertical":
        # Render each character on its own line, right-aligned, descending.
        line_height = max(10, int(font.size * font.line_height))
        char_x = int(x + w - font.size - 4)
        cy = int(y + 4)
        for ch in text:
            draw.text((char_x, cy), ch, fill=color)
            cy += line_height
            if cy > y + h - line_height:
                break
    else:
        anchor = _text_anchor(direction, x, y, w, h)
        # text() supports anchor only on newer Pillow; fall back gracefully.
        try:
            draw.text(
                (int(x + w / 2), int(y + h / 2)),
                text,
                fill=color,
                anchor=anchor,
            )
        except TypeError:
            draw.text((int(x + 4), int(y + h / 2 - font.size / 2)), text, fill=color)


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
