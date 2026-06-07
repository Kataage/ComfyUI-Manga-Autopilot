"""Tests for the Page / Panel / Dialogue models (spec section 14.4-14.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from manga_autopilot.models.page import (
    Dialogue,
    PagePlan,
    PanelPlan,
    ProjectMetadata,
    SoundEffect,
)


def _panel(num: int = 1) -> dict:
    return {
        "panel_number": num,
        "purpose": "establishing shot",
        "shot": "wide",
        "camera_angle": "eye level",
        "characters": ["alice", "bob"],
        "background": "classroom",
        "action": "talking",
        "emotion": "calm",
        "visual_priority": "character",
    }


def test_page_minimal() -> None:
    page = PagePlan(page_number=1, panel_count=2)
    assert page.panel_count == 2
    assert page.panels == []


def test_page_with_panels() -> None:
    page = PagePlan(
        page_number=2,
        summary="climax",
        emotional_goal="tension",
        visual_goal="dark lighting",
        panel_count=3,
        cliffhanger="shadow appears",
        panels=[_panel(1), _panel(2), _panel(3)],
    )
    assert page.summary == "climax"
    assert page.cliffhanger == "shadow appears"
    assert len(page.panels) == 3


def test_page_rejects_too_many_panels() -> None:
    with pytest.raises(ValidationError):
        PagePlan(
            page_number=1,
            panel_count=1,
            panels=[_panel(1), _panel(2)],
        )


def test_panel_dedupes_characters() -> None:
    panel = PanelPlan.model_validate(
        {**_panel(1), "characters": ["alice", "alice", "bob", "bob"]}
    )
    assert panel.characters == ["alice", "bob"]


def test_dialogue_defaults() -> None:
    d = Dialogue(speaker="alice", text="hi")
    assert d.type == "speech"
    assert d.balloon_position == "top"


def test_dialogue_validates_type() -> None:
    with pytest.raises(ValidationError):
        Dialogue(speaker="alice", text="hi", type="shout")  # type: ignore[arg-type]


def test_sfx_intensity_non_negative() -> None:
    with pytest.raises(ValidationError):
        SoundEffect(text="BOOM", intensity=-1)


def test_project_metadata_defaults() -> None:
    meta = ProjectMetadata(title="My Manga")
    assert meta.target_pages == 4
    assert meta.characters == []
    assert meta.created_at <= meta.updated_at
