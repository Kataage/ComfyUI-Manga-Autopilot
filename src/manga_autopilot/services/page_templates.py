"""Built-in page and webtoon layout templates (spec sections 15.2 and 15.3)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from manga_autopilot.models.panel import PanelLayout

TemplateKind = Literal["page", "webtoon"]


@dataclass(frozen=True)
class PanelRect:
    """Rectangle in normalised page coordinates (0..1)."""

    x: float
    y: float
    width: float
    height: float
    z_index: int = 0


@dataclass(frozen=True)
class PageTemplate:
    template_id: str
    name: str
    kind: TemplateKind
    description: str
    page_width: int
    page_height: int
    panels: list[PanelRect] = field(default_factory=list)

    def to_panel_layouts(self, panel_prefix: str = "panel") -> list[PanelLayout]:
        return [
            PanelLayout(
                panel_id=f"{panel_prefix}_{i + 1:02d}",
                x=rect.x * self.page_width,
                y=rect.y * self.page_height,
                width=rect.width * self.page_width,
                height=rect.height * self.page_height,
                z_index=rect.z_index,
            )
            for i, rect in enumerate(self.panels)
        ]


# --------------------------------------------------------------------- page
PAGE_2_PANEL_HORIZONTAL = PageTemplate(
    template_id="page_2_horizontal",
    name="2-panel horizontal",
    kind="page",
    description="Wide panel on top, narrower panel on the bottom.",
    page_width=1200,
    page_height=1600,
    panels=[
        PanelRect(0.05, 0.05, 0.90, 0.45, z_index=0),
        PanelRect(0.05, 0.55, 0.90, 0.40, z_index=1),
    ],
)

PAGE_3_PANEL_T = PageTemplate(
    template_id="page_3_t",
    name="3-panel T",
    kind="page",
    description="Top wide panel with two stacked panels below.",
    page_width=1200,
    page_height=1600,
    panels=[
        PanelRect(0.05, 0.05, 0.90, 0.40, z_index=0),
        PanelRect(0.05, 0.50, 0.43, 0.45, z_index=1),
        PanelRect(0.52, 0.50, 0.43, 0.45, z_index=2),
    ],
)

PAGE_4_PANEL_GRID = PageTemplate(
    template_id="page_4_grid",
    name="4-panel grid",
    kind="page",
    description="2x2 grid of equal panels.",
    page_width=1200,
    page_height=1600,
    panels=[
        PanelRect(0.05, 0.05, 0.43, 0.43, z_index=0),
        PanelRect(0.52, 0.05, 0.43, 0.43, z_index=1),
        PanelRect(0.05, 0.52, 0.43, 0.43, z_index=2),
        PanelRect(0.52, 0.52, 0.43, 0.43, z_index=3),
    ],
)

PAGE_5_PANEL_CINEMATIC = PageTemplate(
    template_id="page_5_cinematic",
    name="5-panel cinematic",
    kind="page",
    description="Establishing shot on top, three mid-shots, and a wide bottom panel.",
    page_width=1200,
    page_height=1600,
    panels=[
        PanelRect(0.05, 0.05, 0.90, 0.30, z_index=0),
        PanelRect(0.05, 0.38, 0.28, 0.27, z_index=1),
        PanelRect(0.36, 0.38, 0.28, 0.27, z_index=2),
        PanelRect(0.67, 0.38, 0.28, 0.27, z_index=3),
        PanelRect(0.05, 0.68, 0.90, 0.27, z_index=4),
    ],
)

PAGE_TEMPLATES: tuple[PageTemplate, ...] = (
    PAGE_2_PANEL_HORIZONTAL,
    PAGE_3_PANEL_T,
    PAGE_4_PANEL_GRID,
    PAGE_5_PANEL_CINEMATIC,
)


# ------------------------------------------------------------------ webtoon
WEBTOON_VERTICAL_3 = PageTemplate(
    template_id="webtoon_vertical_3",
    name="Webtoon vertical 3",
    kind="webtoon",
    description="Three equal vertical slices for a long scrolling page.",
    page_width=800,
    page_height=6000,
    panels=[
        PanelRect(0.05, 0.02, 0.90, 0.31, z_index=0),
        PanelRect(0.05, 0.35, 0.90, 0.30, z_index=1),
        PanelRect(0.05, 0.67, 0.90, 0.31, z_index=2),
    ],
)

WEBTOON_LONG_STRIP = PageTemplate(
    template_id="webtoon_long_strip",
    name="Webtoon long strip",
    kind="webtoon",
    description="Single tall strip with one full-width panel for a hero shot.",
    page_width=800,
    page_height=10000,
    panels=[
        PanelRect(0.05, 0.02, 0.90, 0.96, z_index=0),
    ],
)

WEBTOON_TEMPLATES: tuple[PageTemplate, ...] = (
    WEBTOON_VERTICAL_3,
    WEBTOON_LONG_STRIP,
)


ALL_TEMPLATES: dict[str, PageTemplate] = {t.template_id: t for t in PAGE_TEMPLATES + WEBTOON_TEMPLATES}


def list_templates(kind: TemplateKind | None = None) -> list[PageTemplate]:
    if kind is None:
        return list(ALL_TEMPLATES.values())
    if kind == "page":
        return list(PAGE_TEMPLATES)
    if kind == "webtoon":
        return list(WEBTOON_TEMPLATES)
    raise ValueError(f"unknown template kind: {kind!r}")


def get_template(template_id: str) -> PageTemplate:
    try:
        return ALL_TEMPLATES[template_id]
    except KeyError as exc:
        raise KeyError(f"template not found: {template_id!r}") from exc


def layout_catalog(kind: TemplateKind | None = None) -> list[dict[str, object]]:
    catalog: list[dict[str, object]] = []
    for template in list_templates(kind):
        if template.kind == "webtoon":
            order = sorted(
                range(len(template.panels)),
                key=lambda index: template.panels[index].y,
            )
        else:
            order = sorted(
                range(len(template.panels)),
                key=lambda index: (
                    template.panels[index].y,
                    -template.panels[index].x,
                ),
            )
        catalog.append(
            {
                "layout_id": template.template_id,
                "panel_count": len(template.panels),
                "reading_order": [index + 1 for index in order],
            }
        )
    return catalog


def fallback_grid(
    panel_count: int,
    page_width: int = 1200,
    page_height: int = 1600,
) -> PageTemplate:
    if not 1 <= panel_count <= 24:
        raise ValueError("panel_count must be between 1 and 24")
    if panel_count == 1:
        panels = [PanelRect(0.05, 0.05, 0.90, 0.90)]
    else:
        columns = 2
        rows = math.ceil(panel_count / columns)
        gap = 0.04
        width = 0.43
        height = (0.90 - gap * (rows - 1)) / rows
        panels = []
        for index in range(panel_count):
            row = index // columns
            column = index % columns
            panels.append(
                PanelRect(
                    x=0.05 + column * (width + gap),
                    y=0.05 + row * (height + gap),
                    width=width,
                    height=height,
                    z_index=index,
                )
            )
    return PageTemplate(
        template_id=f"fallback_grid_{panel_count}",
        name=f"Fallback grid {panel_count}",
        kind="page",
        description="Deterministic grid used when no registered layout is selected.",
        page_width=page_width,
        page_height=page_height,
        panels=panels,
    )


__all__ = [
    "TemplateKind",
    "PanelRect",
    "PageTemplate",
    "PAGE_TEMPLATES",
    "WEBTOON_TEMPLATES",
    "ALL_TEMPLATES",
    "list_templates",
    "get_template",
    "layout_catalog",
    "fallback_grid",
]
