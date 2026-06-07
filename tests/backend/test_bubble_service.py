"""Tests for the bubble service."""

from __future__ import annotations

from pathlib import Path

import pytest

from manga_autopilot.models.bubble import SpeechBubble
from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.services.bubble_service import (
    BubbleNotFoundError,
    BubbleService,
)


def _init_project(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.json").write_text("{}", encoding="utf-8")


def test_service_round_trip(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = BubbleService(project_root=tmp_path / "demo")
    bubble = SpeechBubble(id="b1", panel_id="p1", text="hi")
    svc.upsert(bubble)
    listed = svc.list_bubbles()
    assert len(listed) == 1
    assert listed[0].id == "b1"


def test_upsert_replaces_existing(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = BubbleService(project_root=tmp_path / "demo")
    svc.upsert(SpeechBubble(id="b1", panel_id="p1", text="a"))
    svc.upsert(SpeechBubble(id="b1", panel_id="p1", text="b"))
    assert svc.list_bubbles()[0].text == "b"


def test_list_bubbles_filters_by_panel(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = BubbleService(project_root=tmp_path / "demo")
    svc.upsert(SpeechBubble(id="b1", panel_id="p1", text="a"))
    svc.upsert(SpeechBubble(id="b2", panel_id="p2", text="b"))
    assert {b.id for b in svc.list_bubbles("p1")} == {"b1"}


def test_delete_raises_for_unknown(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = BubbleService(project_root=tmp_path / "demo")
    with pytest.raises(BubbleNotFoundError):
        svc.delete("missing")


def test_delete_for_panel_returns_count(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = BubbleService(project_root=tmp_path / "demo")
    svc.upsert(SpeechBubble(id="b1", panel_id="p1", text="a"))
    svc.upsert(SpeechBubble(id="b2", panel_id="p1", text="b"))
    svc.upsert(SpeechBubble(id="b3", panel_id="p2", text="c"))
    assert svc.delete_for_panel("p1") == 2
    assert {b.id for b in svc.list_bubbles()} == {"b3"}


def test_layout_panel_uses_existing_bubbles(tmp_path: Path) -> None:
    _init_project(tmp_path)
    svc = BubbleService(project_root=tmp_path / "demo")
    svc.upsert(SpeechBubble(id="b1", panel_id="p1", text="a", order=1))
    svc.upsert(SpeechBubble(id="b2", panel_id="p1", text="b", order=2))
    panel = PanelLayout(panel_id="p1", x=0, y=0, width=400, height=300)
    placements = svc.layout_panel(panel)
    assert [p.bubble.id for p in placements] == ["b1", "b2"]


def test_corrupt_bubbles_file_raises(tmp_path: Path) -> None:
    _init_project(tmp_path)
    (tmp_path / "demo" / "bubbles.json").write_text("not json", encoding="utf-8")
    svc = BubbleService(project_root=tmp_path / "demo")
    with pytest.raises(ValueError):
        svc.list_bubbles()
