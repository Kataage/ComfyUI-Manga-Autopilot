"""Tests for the page PNG renderer."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from manga_autopilot.models.panel import PanelBorder, PanelLayout
from manga_autopilot.services.page_renderer import render_page_to_png


def test_render_minimal_page(tmp_path: Path) -> None:
    result = render_page_to_png(
        "page_1",
        [
            PanelLayout(panel_id="p1", x=40, y=40, width=300, height=200),
            PanelLayout(
                panel_id="p2",
                x=400,
                y=40,
                width=300,
                height=200,
                border=PanelBorder(color="#ff00ff", width=3.0, radius=10.0),
            ),
        ],
        output_dir=tmp_path,
        page_width=800,
        page_height=600,
    )
    assert result.output_path.exists()
    assert result.panels_drawn == 2
    image = Image.open(result.output_path)
    assert image.size == (800, 600)


def test_render_rejects_bad_page_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        render_page_to_png("intro", [], output_dir=tmp_path)
    with pytest.raises(ValueError):
        render_page_to_png("page_xyz", [], output_dir=tmp_path)


def test_render_skips_offscreen_panels(tmp_path: Path) -> None:
    result = render_page_to_png(
        "page_2",
        [
            PanelLayout(panel_id="inside", x=10, y=10, width=100, height=100),
            PanelLayout(panel_id="outside", x=2000, y=2000, width=100, height=100),
            PanelLayout(panel_id="negative", x=-50, y=-50, width=10, height=10),
        ],
        output_dir=tmp_path,
        page_width=400,
        page_height=400,
    )
    assert result.panels_drawn == 1


def test_render_uses_filename_padding(tmp_path: Path) -> None:
    render_page_to_png("page_42", [], output_dir=tmp_path, page_width=200, page_height=200)
    assert (tmp_path / "page_0042.png").exists()


def test_render_outer_border_can_be_disabled(tmp_path: Path) -> None:
    result = render_page_to_png(
        "page_1",
        [],
        output_dir=tmp_path,
        page_width=200,
        page_height=200,
        outer_border=False,
    )
    image = Image.open(result.output_path)
    # Top-left pixel is the background (no border drawn).
    assert image.getpixel((0, 0)) == (255, 255, 255)
