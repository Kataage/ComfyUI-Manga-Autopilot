"""Render a page to PNG (spec section 20.1).

The renderer draws a white background, an outer page border, and one
bordered rectangle per panel.  Image content is not yet composited — that
arrives once the executor (Epic 9) writes panel images to disk.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.services.panel_renderer import build_render_instructions

log = logging.getLogger(__name__)

PAGE_ID_RE = re.compile(r"^page_(\d{1,5})$")


@dataclass
class PageRenderResult:
    page_id: str
    output_path: Path
    width: int
    height: int
    panels_drawn: int


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"invalid hex color: {value!r}")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def render_page_to_png(
    page_id: str,
    panels: list[PanelLayout],
    *,
    output_dir: str | Path,
    page_width: int = 1200,
    page_height: int = 1600,
    background: str = "#ffffff",
    outer_border: bool = True,
) -> PageRenderResult:
    """Draw a minimal page (background + panel borders) to ``output_dir``.

    The file is written to ``{output_dir}/page_{number}.png``.  The page id
    is expected to be of the form ``page_3``; only the numeric part is used
    to build the filename.
    """

    match = PAGE_ID_RE.match(page_id)
    if not match:
        raise ValueError(
            f"page_id must match the pattern 'page_<number>'; got {page_id!r}"
        )
    page_number = int(match.group(1))
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"page_{page_number:04d}.png"

    image = Image.new("RGB", (page_width, page_height), color=_hex_to_rgb(background))
    draw = ImageDraw.Draw(image)
    if outer_border:
        draw.rectangle(
            [(0, 0), (page_width - 1, page_height - 1)],
            outline=(0, 0, 0),
            width=4,
        )

    drawn = 0
    for layout in panels:
        instr = build_render_instructions(layout, inner_inset=0)
        # Clamp to page bounds; the editor occasionally lets users drag
        # panels slightly outside the page.
        x0 = max(0, int(instr.x))
        y0 = max(0, int(instr.y))
        x1 = min(page_width, int(instr.x + instr.width))
        y1 = min(page_height, int(instr.y + instr.height))
        if x1 <= x0 or y1 <= y0:
            continue
        if instr.border_radius > 0:
            draw.rounded_rectangle(
                [(x0, y0), (x1, y1)],
                radius=int(min(instr.border_radius, (x1 - x0) / 2, (y1 - y0) / 2)),
                outline=_hex_to_rgb(instr.border_color),
                width=max(1, int(instr.border_width)),
            )
        else:
            draw.rectangle(
                [(x0, y0), (x1, y1)],
                outline=_hex_to_rgb(instr.border_color),
                width=max(1, int(instr.border_width)),
            )
        drawn += 1

    image.save(output_path, format="PNG")
    log.info("rendered %s panels for %s -> %s", drawn, page_id, output_path)
    return PageRenderResult(
        page_id=page_id,
        output_path=output_path,
        width=page_width,
        height=page_height,
        panels_drawn=drawn,
    )


__all__ = ["PageRenderResult", "render_page_to_png"]
