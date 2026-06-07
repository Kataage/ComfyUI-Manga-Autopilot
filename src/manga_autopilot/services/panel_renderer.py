"""Panel border + bleed drawing helpers (spec section 15.5 border, 20.1)."""

from __future__ import annotations

from dataclasses import dataclass

from manga_autopilot.models.panel import PanelBorder, PanelLayout

DEFAULT_INNER_INSET_PX = 6


@dataclass
class PanelRenderInstructions:
    """A small, frontend-agnostic description of how to draw one panel."""

    panel_id: str
    x: float
    y: float
    width: float
    height: float
    border_width: float
    border_color: str
    border_radius: float
    bleed: bool
    rotation: float | None
    inner_inset: float


def build_render_instructions(
    layout: PanelLayout,
    *,
    inner_inset: float = DEFAULT_INNER_INSET_PX,
) -> PanelRenderInstructions:
    """Translate a :class:`PanelLayout` into render instructions."""

    return PanelRenderInstructions(
        panel_id=layout.panel_id,
        x=layout.x,
        y=layout.y,
        width=layout.width,
        height=layout.height,
        border_width=layout.border.width,
        border_color=layout.border.color,
        border_radius=layout.border.radius,
        bleed=layout.bleed,
        rotation=layout.rotation,
        inner_inset=max(0.0, inner_inset),
    )


def render_border_svg(instructions: PanelRenderInstructions) -> str:
    """Render a panel border as a single SVG ``<rect>`` string.

    Bleed mode adds a 3mm (12px) oversize outer rect; the renderer is free
    to ignore that flag if its surface has no bleed area.
    """

    bleed = 12.0 if instructions.bleed else 0.0
    x = instructions.x - bleed
    y = instructions.y - bleed
    width = instructions.width + 2 * bleed
    height = instructions.height + 2 * bleed
    rotation = (
        f' transform="rotate({instructions.rotation} '
        f'{instructions.x + instructions.width / 2} '
        f'{instructions.y + instructions.height / 2})"'
        if instructions.rotation is not None
        else ""
    )
    return (
        f'<rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" '
        f'rx="{instructions.border_radius:.2f}" ry="{instructions.border_radius:.2f}" '
        f'fill="none" stroke="{instructions.border_color}" '
        f'stroke-width="{instructions.border_width:.2f}"{rotation} />'
    )


def build_panel_default_border() -> PanelBorder:
    return PanelBorder()


__all__ = [
    "DEFAULT_INNER_INSET_PX",
    "PanelRenderInstructions",
    "build_render_instructions",
    "render_border_svg",
    "build_panel_default_border",
]
