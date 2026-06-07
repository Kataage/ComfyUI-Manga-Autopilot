"""Tests for the bubble auto-placement helper (spec section 19.5)."""

from __future__ import annotations

from manga_autopilot.models.bubble import SpeechBubble, TailTarget
from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.services.bubble_layout import (
    place_bubbles,
    rects_overlap,
)


def _panel() -> PanelLayout:
    return PanelLayout(panel_id="p1", x=0, y=0, width=400, height=300)


def test_rects_overlap() -> None:
    assert rects_overlap(0, 0, 50, 50, 25, 25, 50, 50)
    assert not rects_overlap(0, 0, 50, 50, 100, 100, 50, 50)


def test_place_single_bubble_in_panel() -> None:
    bubble = SpeechBubble(id="b1", panel_id="p1", text="hi", order=1)
    placements = place_bubbles([bubble], _panel())
    assert len(placements) == 1
    p = placements[0]
    assert p.x >= 0
    assert p.y >= 0
    assert p.x + p.width <= 400
    assert p.y + p.height <= 300


def test_place_multiple_bubbles_does_not_overlap() -> None:
    bubbles = [
        SpeechBubble(id=f"b{i}", panel_id="p1", text=f"line {i}", order=i)
        for i in range(1, 4)
    ]
    placements = place_bubbles(bubbles, _panel())
    assert len(placements) == 3
    for i, a in enumerate(placements):
        for b in placements[i + 1 :]:
            assert not rects_overlap(a.x, a.y, a.width, a.height, b.x, b.y, b.width, b.height)


def test_bubble_avoided_near_tail_target() -> None:
    # Tail target near the centre of the panel — the placer should pick a
    # candidate that does not overlap the target safety zone.
    bubble = SpeechBubble(
        id="b1",
        panel_id="p1",
        text="!",
        order=1,
        tail_target=TailTarget(x=200, y=150),
    )
    placements = place_bubbles([bubble], _panel())
    p = placements[0]
    # Bubble rectangle must not contain the tail target.
    contains_target = p.x <= 200 <= p.x + p.width and p.y <= 150 <= p.y + p.height
    assert not contains_target


def test_place_handles_oversized_bubbles() -> None:
    bubble = SpeechBubble(
        id="big",
        panel_id="p1",
        text="x",
        width=900,
        height=900,
        order=1,
    )
    placements = place_bubbles([bubble], _panel())
    assert len(placements) == 1
    # The placer must clamp the bubble to the panel.
    assert placements[0].width <= 400
    assert placements[0].height <= 300


def test_place_returns_confidence_below_one_when_forced() -> None:
    # Three oversized bubbles all want the same anchor; later ones fall back.
    bubbles = [
        SpeechBubble(id=f"b{i}", panel_id="p1", text="x", width=380, height=270, order=i)
        for i in range(3)
    ]
    placements = place_bubbles(bubbles, _panel())
    confidences = sorted(p.confidence for p in placements)
    assert confidences[0] < 1.0
