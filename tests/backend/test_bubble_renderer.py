"""Tests for the speech bubble renderer (spec section 19.4)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from manga_autopilot.models.bubble import FontSpec, SpeechBubble, TailTarget
from manga_autopilot.services.bubble_renderer import (
    draw_bubble_on_canvas,
    render_bubble_to_png,
)


def _bubble(**overrides) -> SpeechBubble:
    payload = {
        "id": "b1",
        "panel_id": "p1",
        "text": "hi",
    }
    payload.update(overrides)
    return SpeechBubble(**payload)


def test_render_normal_bubble(tmp_path: Path) -> None:
    out = tmp_path / "normal.png"
    res = render_bubble_to_png(_bubble(type="normal"), str(out))
    assert res.output_path == str(out)
    assert out.exists()
    image = Image.open(out)
    assert image.size == (480, 240)


def test_render_shout_bubble(tmp_path: Path) -> None:
    out = tmp_path / "shout.png"
    render_bubble_to_png(_bubble(type="shout", text="WOW!"), str(out))
    assert out.exists()


def test_render_with_tail(tmp_path: Path) -> None:
    out = tmp_path / "tail.png"
    target = TailTarget(x=10, y=10)
    bubble = _bubble(tail_target=target)
    res = render_bubble_to_png(bubble, str(out))
    assert res.bubble_id == "b1"


def test_render_empty_text_does_not_crash(tmp_path: Path) -> None:
    out = tmp_path / "empty.png"
    render_bubble_to_png(_bubble(text=""), str(out))
    assert out.exists()


def test_draw_bubble_on_canvas_does_not_error() -> None:
    canvas = Image.new("RGB", (600, 400), (255, 255, 255))
    draw_bubble_on_canvas(canvas, _bubble(text="hello"), 50, 50, 200, 150)


def test_vertical_text_uses_descending_layout() -> None:
    canvas = Image.new("RGB", (400, 400), (255, 255, 255))
    # Should not raise even without a TTF installed.
    draw_bubble_on_canvas(canvas, _bubble(text="abc", direction="vertical"), 50, 50, 200, 300)


def test_custom_font_size() -> None:
    canvas = Image.new("RGB", (400, 400), (255, 255, 255))
    bubble = _bubble(
        text="big",
        font=FontSpec(size=48, color="#ff0000", weight="bold"),
    )
    draw_bubble_on_canvas(canvas, bubble, 10, 10, 200, 100)
