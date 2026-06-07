"""Tests for the image-compositing path in the page renderer."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.services.page_renderer import render_page_to_png


def _make_image(path: Path, color: tuple[int, int, int]) -> Path:
    img = Image.new("RGB", (200, 200), color)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return path


def test_render_page_composites_image(tmp_path: Path):
    image_path = _make_image(tmp_path / "panel.png", (255, 0, 0))
    panel = PanelLayout(
        panel_id="p1",
        x=20.0,
        y=20.0,
        width=300.0,
        height=300.0,
        image_path=str(image_path),
    )
    result = render_page_to_png("page_1", [panel], output_dir=tmp_path)
    assert result.output_path.exists()
    assert result.images_composited == 1
    rendered = Image.open(result.output_path).convert("RGB")
    # The red panel content should be visible in the rendered area.
    assert rendered.getpixel((100, 100)) == (255, 0, 0)


def test_render_page_missing_image_falls_back_to_border(tmp_path: Path):
    panel = PanelLayout(
        panel_id="p1",
        x=20.0,
        y=20.0,
        width=200.0,
        height=200.0,
        image_path=str(tmp_path / "missing.png"),
    )
    result = render_page_to_png("page_1", [panel], output_dir=tmp_path)
    assert result.panels_drawn == 1
    assert result.images_composited == 0
    assert result.output_path.exists()


def test_render_page_cover_fit_crops(tmp_path: Path):
    image_path = _make_image(tmp_path / "wide.png", (0, 255, 0))
    panel = PanelLayout(
        panel_id="p1",
        x=0.0,
        y=0.0,
        width=100.0,
        height=300.0,
        image_path=str(image_path),
        image_fit="cover",
    )
    result = render_page_to_png("page_1", [panel], output_dir=tmp_path)
    rendered = Image.open(result.output_path).convert("RGB")
    # Some part of the panel should still be green from the image.
    green_count = sum(1 for x in range(100) for y in range(300) if rendered.getpixel((x, y)) == (0, 255, 0))
    assert green_count > 0


def test_render_page_contain_fit_centers(tmp_path: Path):
    image_path = _make_image(tmp_path / "wide.png", (0, 0, 255))
    panel = PanelLayout(
        panel_id="p1",
        x=0.0,
        y=0.0,
        width=300.0,
        height=100.0,
        image_path=str(image_path),
        image_fit="contain",
    )
    result = render_page_to_png("page_1", [panel], output_dir=tmp_path)
    rendered = Image.open(result.output_path).convert("RGB")
    blue_count = sum(1 for x in range(300) for y in range(100) if rendered.getpixel((x, y)) == (0, 0, 255))
    assert blue_count > 0


def test_panel_layout_rejects_bad_image_fit():
    with pytest.raises(ValueError):
        PanelLayout(panel_id="p", image_fit="bogus")
