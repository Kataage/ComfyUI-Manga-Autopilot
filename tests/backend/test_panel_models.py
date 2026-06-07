"""Tests for the Panel / PanelLayout models and JSON persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from manga_autopilot.models.page import PanelPlan
from manga_autopilot.models.panel import (
    PanelBorder,
    PanelLayout,
    PanelRecord,
    load_panel_records,
    write_panel_records,
)


def _plan() -> PanelPlan:
    return PanelPlan(
        panel_number=1,
        purpose="establishing",
        characters=["alice"],
    )


def test_panel_layout_defaults() -> None:
    layout = PanelLayout(panel_id="p1")
    assert layout.border.color == "#000000"
    assert layout.bleed is False
    assert layout.z_index == 0


def test_panel_layout_id_regex() -> None:
    with pytest.raises(ValidationError):
        PanelLayout(panel_id="with space")


def test_panel_border_color_must_be_hex() -> None:
    with pytest.raises(ValidationError):
        PanelBorder(color="red")


def test_panel_record_status_enum() -> None:
    rec = PanelRecord(panel_id="p1", page_number=1, plan=_plan())
    assert rec.status == "draft"
    with pytest.raises(ValidationError):
        PanelRecord(panel_id="p1", page_number=1, plan=_plan(), status="unknown")


def test_panel_record_round_trip(tmp_path: Path) -> None:
    rec = PanelRecord(
        panel_id="p1",
        page_number=2,
        plan=_plan(),
        layout=PanelLayout(panel_id="p1", x=10, y=20, width=400, height=300),
        workflow_id="anime_t2i_default",
    )
    out = tmp_path / "panels.json"
    write_panel_records(out, [rec])
    assert json.loads(out.read_text("utf-8"))[0]["panel_id"] == "p1"

    loaded = load_panel_records(out)
    assert len(loaded) == 1
    assert loaded[0].layout is not None
    assert loaded[0].layout.width == 400


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_panel_records(tmp_path / "missing.json") == []


def test_load_rejects_non_array(tmp_path: Path) -> None:
    p = tmp_path / "panels.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_panel_records(p)


def test_panel_rotation_optional() -> None:
    rec = PanelRecord(
        panel_id="p1",
        page_number=1,
        plan=_plan(),
        layout=PanelLayout(panel_id="p1", rotation=15.0),
    )
    assert rec.layout.rotation == 15.0
