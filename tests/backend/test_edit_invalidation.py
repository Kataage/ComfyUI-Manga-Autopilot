"""Edit invalidation (plan Task 7, steps 5-6).

An edit marks downstream work stale. It never deletes history and never starts
GPU work: deciding what to regenerate is the user's call, not a side effect of
typing.
"""

from __future__ import annotations

import pytest

from manga_autopilot.models.page import PanelPlan
from manga_autopilot.models.panel import PanelRecord
from manga_autopilot.services.edit_invalidation import (
    BUBBLES,
    EXPORTS,
    PAGE_RENDER,
    PANEL_IMAGES,
    EditDescriptor,
    InvalidationResult,
    apply_invalidation,
    compute_invalidation,
)
from manga_autopilot.services.review_gate import ARTWORK_EARLY, ARTWORK_FINAL, STORYBOARD


def _record(page_number: int, panel_number: int, *, status: str = "generated") -> PanelRecord:
    return PanelRecord(
        panel_id=f"p{page_number}_{panel_number:02d}",
        page_number=page_number,
        plan=PanelPlan(panel_number=panel_number, purpose="beat", shot="medium"),
        status=status,
        image_path=f"assets/panels/p{page_number}_{panel_number:02d}.png",
    )


PANELS = [_record(1, 1), _record(1, 2), _record(2, 1), _record(2, 2), _record(3, 1)]


# --------------------------------------------------------------- dialogue


def test_dialogue_edit_marks_lettering_and_page_export_stale_only() -> None:
    result = compute_invalidation(
        EditDescriptor(kind="dialogue", page_number=2, panel_number=1), PANELS
    )

    assert result.stale_stages == {BUBBLES, PAGE_RENDER, EXPORTS}
    assert result.stale_panel_images == set()


def test_dialogue_edit_reopens_only_the_final_artwork_gate() -> None:
    result = compute_invalidation(
        EditDescriptor(kind="dialogue", page_number=2, panel_number=1), PANELS
    )

    assert result.stale_gates == {ARTWORK_FINAL}


# -------------------------------------------------------------- image only


def test_image_only_edit_invalidates_just_that_panel() -> None:
    result = compute_invalidation(
        EditDescriptor(kind="image_only", page_number=2, panel_number=1), PANELS
    )

    assert result.stale_panel_images == {"p2_01"}
    assert result.stale_stages == {PANEL_IMAGES, PAGE_RENDER, EXPORTS}
    assert BUBBLES not in result.stale_stages


def test_image_only_edit_without_a_panel_targets_the_whole_page() -> None:
    result = compute_invalidation(EditDescriptor(kind="image_only", page_number=1), PANELS)

    assert result.stale_panel_images == {"p1_01", "p1_02"}


# ------------------------------------------------------------------ layout


def test_layout_edit_invalidates_every_panel_on_the_page() -> None:
    result = compute_invalidation(
        EditDescriptor(kind="layout", page_number=1, panel_number=1), PANELS
    )

    assert result.stale_panel_images == {"p1_01", "p1_02"}
    assert result.stale_stages == {PANEL_IMAGES, BUBBLES, PAGE_RENDER, EXPORTS}
    assert STORYBOARD in result.stale_gates


# -------------------------------------------------------------- continuity


def test_continuity_edit_invalidates_this_panel_and_everything_after_it() -> None:
    result = compute_invalidation(
        EditDescriptor(kind="continuity", page_number=2, panel_number=1), PANELS
    )

    assert result.stale_panel_images == {"p2_01", "p2_02", "p3_01"}
    assert "p1_01" not in result.stale_panel_images
    assert result.stale_stages == {PANEL_IMAGES, BUBBLES, PAGE_RENDER, EXPORTS}
    assert result.stale_gates == {STORYBOARD, ARTWORK_EARLY, ARTWORK_FINAL}


def test_a_character_edit_invalidates_every_panel_that_character_appears_in() -> None:
    panels = [
        _record(1, 1),
        _record(1, 2),
        _record(2, 1),
    ]
    panels[0].plan.characters = ["hero_a"]
    panels[1].plan.characters = ["hero_b"]
    panels[2].plan.characters = ["hero_a", "hero_b"]

    result = compute_invalidation(
        EditDescriptor(kind="character", character_id="hero_a"), panels
    )

    assert result.stale_panel_images == {"p1_01", "p2_01"}


def test_a_character_edit_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="character_id"):
        compute_invalidation(EditDescriptor(kind="character"), PANELS)


def test_an_unknown_edit_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="colouring"):
        compute_invalidation(EditDescriptor(kind="colouring"), PANELS)


# ------------------------------------------------------------- application


def test_applying_invalidation_marks_panels_draft_without_losing_history() -> None:
    panels = [_record(1, 1), _record(2, 1)]
    panels[0].history.append({"kind": "autopilot_generation", "job_id": "j1"})
    original_image = panels[0].image_path
    result = compute_invalidation(EditDescriptor(kind="image_only", page_number=1), panels)

    changed = apply_invalidation(result, panels)

    assert [p.panel_id for p in changed] == ["p1_01"]
    assert panels[0].status == "draft"
    assert panels[0].image_path == original_image, "the old image stays until it is replaced"
    assert panels[0].history[0]["job_id"] == "j1"
    assert panels[0].history[-1]["kind"] == "invalidated"
    assert panels[1].status == "generated"


def test_applying_invalidation_starts_no_work() -> None:
    panels = [_record(1, 1)]
    result = compute_invalidation(EditDescriptor(kind="continuity", page_number=1), panels)

    changed = apply_invalidation(result, panels)

    # The only observable effect is on the records handed in.
    assert changed == [panels[0]]
    assert panels[0].status == "draft"


def test_applying_a_dialogue_invalidation_leaves_panel_status_alone() -> None:
    panels = [_record(1, 1)]
    result = compute_invalidation(EditDescriptor(kind="dialogue", page_number=1), panels)

    changed = apply_invalidation(result, panels)

    assert changed == []
    assert panels[0].status == "generated"


def test_result_is_reported_as_plain_data() -> None:
    result = compute_invalidation(EditDescriptor(kind="dialogue", page_number=1), PANELS)

    assert isinstance(result, InvalidationResult)
    assert result.to_dict()["stale_stages"] == sorted({BUBBLES, PAGE_RENDER, EXPORTS})
