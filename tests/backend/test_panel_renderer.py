"""Tests for the panel border/bleed renderer helpers."""

from __future__ import annotations

from manga_autopilot.models.panel import PanelBorder, PanelLayout
from manga_autopilot.services.panel_renderer import (
    PanelRenderInstructions,
    build_panel_default_border,
    build_render_instructions,
    render_border_svg,
)


def test_default_border() -> None:
    border = build_panel_default_border()
    assert border.width == 2.0
    assert border.color == "#000000"


def test_build_render_instructions() -> None:
    layout = PanelLayout(
        panel_id="p1",
        x=10,
        y=20,
        width=400,
        height=300,
        border=PanelBorder(width=4.0, color="#ff00ff", radius=12.0),
        rotation=15.0,
    )
    instr = build_render_instructions(layout, inner_inset=8)
    assert isinstance(instr, PanelRenderInstructions)
    assert instr.border_color == "#ff00ff"
    assert instr.border_radius == 12.0
    assert instr.rotation == 15.0
    assert instr.inner_inset == 8


def test_render_border_svg_basic() -> None:
    layout = PanelLayout(panel_id="p1", x=0, y=0, width=400, height=300)
    instr = build_render_instructions(layout)
    svg = render_border_svg(instr)
    assert svg.startswith("<rect ")
    assert 'stroke="#000000"' in svg
    assert 'stroke-width="2.00"' in svg


def test_render_border_svg_with_rotation() -> None:
    layout = PanelLayout(panel_id="p1", x=0, y=0, width=100, height=100, rotation=45.0)
    instr = build_render_instructions(layout)
    svg = render_border_svg(instr)
    # Float formatting uses Python's default repr (e.g. 45.0) at 2dp-ish.
    assert "rotate(" in svg and "50.0" in svg and "50.0" in svg
    assert "transform=" in svg


def test_render_border_svg_with_bleed() -> None:
    layout = PanelLayout(panel_id="p1", x=100, y=100, width=400, height=300, bleed=True)
    instr = build_render_instructions(layout)
    svg = render_border_svg(instr)
    # Bleed adds 12px on every side.
    assert 'x="88.00"' in svg
    assert 'y="88.00"' in svg
    assert 'width="424.00"' in svg
    assert 'height="324.00"' in svg


def test_render_border_svg_with_radius() -> None:
    layout = PanelLayout(
        panel_id="p1",
        x=0,
        y=0,
        width=200,
        height=150,
        border=PanelBorder(radius=8.0),
    )
    instr = build_render_instructions(layout)
    svg = render_border_svg(instr)
    assert 'rx="8.00"' in svg
    assert 'ry="8.00"' in svg
