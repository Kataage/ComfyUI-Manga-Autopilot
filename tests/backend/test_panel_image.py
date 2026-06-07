"""Tests for the panel image placement model and helpers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from manga_autopilot.services.panel_image import (
    PanelImagePlacement,
    apply_crop_origin,
    compute_render_rect,
)


def _placement(**overrides) -> PanelImagePlacement:
    payload = {
        "panel_id": "p1",
        "image_path": "img.png",
    }
    payload.update(overrides)
    return PanelImagePlacement(**payload)


def test_default_placement() -> None:
    p = _placement()
    assert p.fit == "cover"
    assert p.crop_x == 0.0
    assert p.crop_w == 1.0


def test_unknown_fit_rejected() -> None:
    with pytest.raises(ValidationError):
        _placement(fit="stretch")  # type: ignore[arg-type]


def test_crop_bounds_validated() -> None:
    with pytest.raises(ValidationError):
        _placement(crop_w=0.0)
    with pytest.raises(ValidationError):
        _placement(crop_w=1.5)


def test_compute_render_rect_cover() -> None:
    rect = compute_render_rect(
        panel_w=400, panel_h=300, image_w=1000, image_h=500, fit="cover"
    )
    x, y, w, h = rect
    # 400/1000 = 0.4, 300/500 = 0.6 -> cover uses max (0.6).
    assert round(w, 2) == 600.0
    assert round(h, 2) == 300.0
    assert round(x, 2) == -100.0  # overflows left/right equally
    assert round(y, 2) == 0.0


def test_compute_render_rect_contain() -> None:
    rect = compute_render_rect(
        panel_w=400, panel_h=300, image_w=1000, image_h=500, fit="contain"
    )
    x, y, w, h = rect
    # min(0.4, 0.6) = 0.4
    assert round(w, 2) == 400.0
    assert round(h, 2) == 200.0
    assert round(x, 2) == 0.0
    assert round(y, 2) == 50.0


def test_compute_render_rect_fill() -> None:
    rect = compute_render_rect(
        panel_w=400, panel_h=300, image_w=1000, image_h=500, fit="fill"
    )
    assert rect == (0.0, 0.0, 400.0, 300.0)


def test_compute_render_rect_offsets() -> None:
    rect = compute_render_rect(
        panel_w=400,
        panel_h=300,
        image_w=1000,
        image_h=500,
        fit="cover",
        offset_x=0.5,
        offset_y=-0.25,
    )
    # cover scale = 0.6, draw 600x300, then shift by 0.5*400 and -0.25*300
    x, y, w, h = rect
    assert round(w, 2) == 600.0
    assert round(h, 2) == 300.0
    assert round(x, 2) == 100.0
    assert round(y, 2) == -75.0


def test_apply_crop_origin() -> None:
    p = _placement(crop_x=0.1, crop_y=0.2, crop_w=0.5, crop_h=0.5)
    x, y, w, h = apply_crop_origin(p, (1000, 500))
    assert (x, y, w, h) == (100.0, 100.0, 500.0, 250.0)


def test_invalid_dimensions_raise() -> None:
    with pytest.raises(ValueError):
        compute_render_rect(panel_w=0, panel_h=100, image_w=10, image_h=10)
    with pytest.raises(ValueError):
        compute_render_rect(panel_w=100, panel_h=100, image_w=0, image_h=10)
