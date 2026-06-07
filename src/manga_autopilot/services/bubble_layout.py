"""Speech bubble layout + auto-placement (spec section 19.5)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from manga_autopilot.models.bubble import SpeechBubble
from manga_autopilot.models.panel import PanelLayout


@dataclass
class BubblePlacement:
    bubble: SpeechBubble
    x: float
    y: float
    width: float
    height: float
    confidence: float = 1.0  # 1.0 = perfect fit, < 1.0 = nudged


def rects_overlap(
    ax: float, ay: float, aw: float, ah: float, bx: float, by: float, bw: float, bh: float
) -> bool:
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def place_bubbles(
    bubbles: Iterable[SpeechBubble],
    panel: PanelLayout,
    *,
    margin: float = 8.0,
) -> list[BubblePlacement]:
    """Auto-place bubbles inside a panel, honouring spec 19.5 rules.

    - Avoid the tail-target safety zone (typically a face/character).
    - Keep bubbles inside the panel margin.
    - Bubbles must not overlap previously placed bubbles.
    - Order by ``bubble.order`` (reading order).
    """

    sorted_bubbles = sorted(bubbles, key=lambda b: (b.order, b.id))
    placements: list[BubblePlacement] = []
    for bubble in sorted_bubbles:
        bw = min(bubble.width, max(40.0, panel.width - 2 * margin))
        bh = min(bubble.height, max(30.0, panel.height - 2 * margin))
        placed: BubblePlacement | None = None
        for ax, ay in _scan_anchors(panel, bw, bh, margin, placements, bubble):
            placed = BubblePlacement(bubble=bubble, x=ax, y=ay, width=bw, height=bh)
            break
        if placed is None:
            # Fallback: top-left inside the panel, accept a collision.
            ax = panel.x + margin
            ay = panel.y + margin
            placed = BubblePlacement(
                bubble=bubble,
                x=ax,
                y=ay,
                width=bw,
                height=bh,
                confidence=0.3,
            )
        placements.append(placed)
    return placements


def _scan_anchors(
    panel: PanelLayout,
    bw: float,
    bh: float,
    margin: float,
    existing: list[BubblePlacement],
    bubble: SpeechBubble,
    step: float = 8.0,
) -> Iterable[tuple[float, float]]:
    """Yield collision-free anchors row by row (top -> bottom, right -> left)."""

    inner_w = panel.width - 2 * margin - bw
    inner_h = panel.height - 2 * margin - bh
    if inner_w < 0 or inner_h < 0:
        return
    # Iterate y from top, x from right.
    y = 0.0
    while y <= inner_h + 1e-6:
        x = inner_w
        while x >= -1e-6:
            ax = panel.x + margin + x
            ay = panel.y + margin + y
            if not _collides_with_existing(ax, ay, bw, bh, existing):
                if not _inside_tail_safety_zone(bubble, ax, ay, bw, bh):
                    yield ax, ay
                    return
            x -= step
        y += step


def _collides_with_existing(x: float, y: float, w: float, h: float, existing: list[BubblePlacement]) -> bool:
    for other in existing:
        if rects_overlap(x, y, w, h, other.x, other.y, other.width, other.height):
            return True
    return False


def _inside_tail_safety_zone(
    bubble: SpeechBubble,
    x: float,
    y: float,
    w: float,
    h: float,
    radius: float = 40.0,
) -> bool:
    """Bubbles should not be placed on top of the tail target (a face)."""

    target = bubble.tail_target
    if target is None:
        return False
    return (
        x <= target.x <= x + w
        and y <= target.y <= y + h
        and abs(target.x - (x + w / 2)) < radius
        and abs(target.y - (y + h / 2)) < radius
    )


__all__ = ["BubblePlacement", "place_bubbles", "rects_overlap"]
