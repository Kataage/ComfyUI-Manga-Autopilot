"""Work out what an edit made stale.

Editing something upstream does not silently trigger a regeneration. This module
answers "what is now out of date?" and marks it; the user decides what to spend
GPU time on. Nothing here deletes an image, drops history, or queues work.

Panel images are only marked stale for edits that actually change the picture.
Rewriting a line of dialogue changes lettering and everything composited after
it, not the artwork underneath.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from manga_autopilot.models.panel import PanelRecord
from manga_autopilot.services.review_gate import ARTWORK_EARLY, ARTWORK_FINAL, STORYBOARD

log = logging.getLogger(__name__)

PANEL_IMAGES = "panel_images"
BUBBLES = "bubbles"
PAGE_RENDER = "page_render"
EXPORTS = "exports"

#: Everything downstream of a panel image, in pipeline order.
DOWNSTREAM_OF_IMAGE: frozenset[str] = frozenset({PAGE_RENDER, EXPORTS})

EDIT_KINDS: tuple[str, ...] = ("dialogue", "image_only", "layout", "continuity", "character")


@dataclass(frozen=True)
class EditDescriptor:
    """What the user changed.

    `page_number` and `panel_number` narrow the blast radius; omitting the panel
    widens an edit to the whole page.
    """

    kind: str
    page_number: int | None = None
    panel_number: int | None = None
    character_id: str = ""


@dataclass(frozen=True)
class InvalidationResult:
    """What the edit made stale."""

    stale_stages: set[str] = field(default_factory=set)
    stale_panel_images: set[str] = field(default_factory=set)
    stale_gates: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "stale_stages": sorted(self.stale_stages),
            "stale_panel_images": sorted(self.stale_panel_images),
            "stale_gates": sorted(self.stale_gates),
        }


def _targets_panel(record: PanelRecord, edit: EditDescriptor) -> bool:
    if edit.page_number is not None and record.page_number != edit.page_number:
        return False
    if edit.panel_number is not None and record.plan.panel_number != edit.panel_number:
        return False
    return True


def _at_or_after(record: PanelRecord, edit: EditDescriptor) -> bool:
    """Whether `record` comes at or after the edited point in reading order."""
    if edit.page_number is None:
        return True
    if record.page_number > edit.page_number:
        return True
    if record.page_number < edit.page_number:
        return False
    if edit.panel_number is None:
        return True
    return record.plan.panel_number >= edit.panel_number


def compute_invalidation(
    edit: EditDescriptor,
    panels: Sequence[PanelRecord],
) -> InvalidationResult:
    """Return what `edit` made stale across `panels`.

    Raises:
        ValueError: the edit kind is unknown, or a character edit names no character.
    """
    if edit.kind not in EDIT_KINDS:
        raise ValueError(f"unknown edit kind: {edit.kind!r}; expected one of {list(EDIT_KINDS)}")

    if edit.kind == "dialogue":
        # The artwork is untouched; only lettering and what is composited on top.
        return InvalidationResult(
            stale_stages={BUBBLES, *DOWNSTREAM_OF_IMAGE},
            stale_gates={ARTWORK_FINAL},
        )

    if edit.kind == "image_only":
        images = {r.panel_id for r in panels if _targets_panel(r, edit)}
        return InvalidationResult(
            stale_stages={PANEL_IMAGES, *DOWNSTREAM_OF_IMAGE},
            stale_panel_images=images,
            stale_gates={ARTWORK_FINAL},
        )

    if edit.kind == "layout":
        # A layout change moves every panel on the page, so all of them are recut.
        page_edit = EditDescriptor(kind="layout", page_number=edit.page_number)
        images = {r.panel_id for r in panels if _targets_panel(r, page_edit)}
        return InvalidationResult(
            stale_stages={PANEL_IMAGES, BUBBLES, *DOWNSTREAM_OF_IMAGE},
            stale_panel_images=images,
            stale_gates={STORYBOARD, ARTWORK_FINAL},
        )

    if edit.kind == "continuity":
        # Scene state flows forward, so everything from here on is suspect.
        images = {r.panel_id for r in panels if _at_or_after(r, edit)}
        return InvalidationResult(
            stale_stages={PANEL_IMAGES, BUBBLES, *DOWNSTREAM_OF_IMAGE},
            stale_panel_images=images,
            stale_gates={STORYBOARD, ARTWORK_EARLY, ARTWORK_FINAL},
        )

    # character
    if not edit.character_id:
        raise ValueError("a character edit must name a character_id")
    images = {r.panel_id for r in panels if edit.character_id in r.plan.characters}
    return InvalidationResult(
        stale_stages={PANEL_IMAGES, *DOWNSTREAM_OF_IMAGE},
        stale_panel_images=images,
        stale_gates={ARTWORK_EARLY, ARTWORK_FINAL},
    )


def apply_invalidation(
    result: InvalidationResult,
    panels: Sequence[PanelRecord],
) -> list[PanelRecord]:
    """Mark the stale panels as needing work and return the ones that changed.

    The existing image path and the full history are kept: the old artwork stays
    visible until something replaces it, and the audit trail gains an entry
    rather than losing one. No generation is started here.
    """
    changed: list[PanelRecord] = []
    now = datetime.now(timezone.utc)
    for record in panels:
        if record.panel_id not in result.stale_panel_images:
            continue
        if record.status == "draft":
            continue
        record.history.append(
            {
                "kind": "invalidated",
                "at": now.isoformat(),
                "previous_status": record.status,
                "stale_stages": sorted(result.stale_stages),
            }
        )
        record.status = "draft"
        record.updated_at = now
        changed.append(record)

    if changed:
        log.info(
            "invalidated %d panel(s); stages now stale: %s",
            len(changed),
            ", ".join(sorted(result.stale_stages)),
        )
    return changed


__all__ = [
    "BUBBLES",
    "DOWNSTREAM_OF_IMAGE",
    "EDIT_KINDS",
    "EXPORTS",
    "PAGE_RENDER",
    "PANEL_IMAGES",
    "EditDescriptor",
    "InvalidationResult",
    "apply_invalidation",
    "compute_invalidation",
]
