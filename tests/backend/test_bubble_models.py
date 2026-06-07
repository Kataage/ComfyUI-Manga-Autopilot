"""Tests for the SpeechBubble + FontSpec models (spec 19.2/19.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from manga_autopilot.models.bubble import (
    BUBBLE_ID_RE,
    FontSpec,
    SpeechBubble,
    TailTarget,
    bubble_storage_filename,
)


def _bubble(**overrides) -> SpeechBubble:
    payload = {
        "id": "b1",
        "panel_id": "p1",
        "text": "Hello",
    }
    payload.update(overrides)
    return SpeechBubble(**payload)


def test_bubble_defaults() -> None:
    b = _bubble()
    assert b.type == "normal"
    assert b.direction == "vertical"
    assert b.font.family == "NotoSansJP"
    assert b.tail_target is None


def test_bubble_id_regex() -> None:
    assert BUBBLE_ID_RE.fullmatch("b1")
    assert not BUBBLE_ID_RE.fullmatch("with space")
    assert not BUBBLE_ID_RE.fullmatch("")


def test_bubble_rejects_invalid_id() -> None:
    with pytest.raises(ValidationError):
        _bubble(id="with space")
    with pytest.raises(ValidationError):
        _bubble(panel_id="x" * 65)


def test_bubble_type_validated() -> None:
    with pytest.raises(ValidationError):
        _bubble(type="bubble")  # type: ignore[arg-type]


def test_bubble_direction_validated() -> None:
    with pytest.raises(ValidationError):
        _bubble(direction="diagonal")  # type: ignore[arg-type]


def test_font_color_must_be_hex() -> None:
    with pytest.raises(ValidationError):
        _bubble(font=FontSpec(color="navy"))


def test_font_size_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _bubble(font=FontSpec(size=0))


def test_tail_target_arbitrary() -> None:
    target = TailTarget(x=10, y=20)
    b = _bubble(tail_target=target)
    assert b.tail_target == target
    assert b.tail_target is not None
    assert b.tail_target.x == 10


def test_character_count() -> None:
    assert _bubble(text="abcdef").character_count() == 6
    assert _bubble(text="").character_count() == 0


def test_bubble_storage_filename() -> None:
    assert bubble_storage_filename() == "bubbles.json"


def test_long_text_rejected() -> None:
    with pytest.raises(ValidationError):
        _bubble(text="x" * 2000)
