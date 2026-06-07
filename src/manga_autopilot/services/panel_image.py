"""Image placement on a panel (spec section 20.1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

FitMode = Literal["cover", "contain", "fill", "none"]


class PanelImagePlacement(BaseModel):
    panel_id: str = Field(min_length=1, max_length=64)
    image_path: str = Field(min_length=1, max_length=512)
    fit: FitMode = "cover"
    crop_x: float = Field(default=0.0, ge=0.0, le=1.0)
    crop_y: float = Field(default=0.0, ge=0.0, le=1.0)
    crop_w: float = Field(default=1.0, ge=0.01, le=1.0)
    crop_h: float = Field(default=1.0, ge=0.01, le=1.0)
    offset_x: float = Field(default=0.0, ge=-1.0, le=1.0)
    offset_y: float = Field(default=0.0, ge=-1.0, le=1.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("crop_w", "crop_h")
    @classmethod
    def _crop_within_bounds(cls, value: float) -> float:
        if value <= 0 or value > 1.0:
            raise ValueError("crop dimensions must be within (0, 1]")
        return value


def compute_render_rect(
    *,
    panel_w: float,
    panel_h: float,
    image_w: float,
    image_h: float,
    fit: FitMode = "cover",
    crop_x: float = 0.0,
    crop_y: float = 0.0,
    crop_w: float = 1.0,
    crop_h: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
) -> tuple[float, float, float, float]:
    """Return the (x, y, width, height) at which to draw the cropped image.

    ``cover`` fills the panel by cropping the image (default), ``contain``
    letterboxes, ``fill`` stretches, ``none`` draws at the source size
    centered on the panel.
    """

    if panel_w <= 0 or panel_h <= 0 or image_w <= 0 or image_h <= 0:
        raise ValueError("panel and image dimensions must be positive")

    cropped_w = image_w * crop_w
    cropped_h = image_h * crop_h
    if fit == "cover":
        scale = max(panel_w / cropped_w, panel_h / cropped_h)
        draw_w = cropped_w * scale
        draw_h = cropped_h * scale
        x = (panel_w - draw_w) / 2 + offset_x * panel_w
        y = (panel_h - draw_h) / 2 + offset_y * panel_h
        return (x, y, draw_w, draw_h)
    if fit == "contain":
        scale = min(panel_w / cropped_w, panel_h / cropped_h)
        draw_w = cropped_w * scale
        draw_h = cropped_h * scale
        x = (panel_w - draw_w) / 2 + offset_x * panel_w
        y = (panel_h - draw_h) / 2 + offset_y * panel_h
        return (x, y, draw_w, draw_h)
    if fit == "fill":
        return (0.0, 0.0, panel_w, panel_h)
    # none: place at original size, centered
    scale = min(1.0, panel_w / cropped_w, panel_h / cropped_h)
    draw_w = cropped_w * scale
    draw_h = cropped_h * scale
    x = (panel_w - draw_w) / 2 + offset_x * panel_w
    y = (panel_h - draw_h) / 2 + offset_y * panel_h
    return (x, y, draw_w, draw_h)


def apply_crop_origin(
    placement: PanelImagePlacement,
    image_size: tuple[int, int],
) -> tuple[float, float, float, float]:
    """Return the absolute (x, y, w, h) inside the source image to crop from."""

    image_w, image_h = image_size
    return (
        placement.crop_x * image_w,
        placement.crop_y * image_h,
        placement.crop_w * image_w,
        placement.crop_h * image_h,
    )


__all__ = [
    "FitMode",
    "PanelImagePlacement",
    "compute_render_rect",
    "apply_crop_origin",
]
