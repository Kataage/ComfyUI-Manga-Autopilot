"""Render a page to PNG (spec section 20.1).

The renderer draws a white background, an outer page border, and one bordered
rectangle per panel.  When a panel carries an ``image_path`` (typically
written by the executor in Epic 9), the image is composited inside the
panel using one of the supported fits:

- ``cover``   (default)  scale + crop to fill the panel, centered
- ``contain`` scale to fit while preserving aspect ratio
- ``stretch`` scale non-uniformly to fill the panel exactly
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

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
    images_composited: int


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"invalid hex color: {value!r}")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _resolve_image(image_path: str | None) -> Image.Image | None:
    """Load a panel image from disk.  Returns ``None`` on any failure."""

    if not image_path:
        return None
    try:
        with Image.open(image_path) as img:
            return img.convert("RGBA").copy()
    except FileNotFoundError:
        log.info("panel image_path %s does not exist; rendering empty panel", image_path)
        return None
    except Exception as exc:  # pragma: no cover - PIL raises a wide variety
        log.warning("could not load panel image %s: %s", image_path, exc)
        return None


def _composite(
    canvas: Image.Image,
    panel: PanelLayout,
    box: tuple[int, int, int, int],
) -> bool:
    """Composite ``panel.image_path`` into ``box`` on ``canvas``.  Returns success."""

    img = _resolve_image(panel.image_path)
    if img is None:
        return False
    x0, y0, x1, y1 = box
    target_w = max(1, x1 - x0)
    target_h = max(1, y1 - y0)
    fit = panel.image_fit
    try:
        if fit == "cover":
            scaled = ImageOps.fit(img, (target_w, target_h), method=Image.LANCZOS, centering=(0.5, 0.5))
        elif fit == "contain":
            scaled = ImageOps.contain(img, (target_w, target_h), method=Image.LANCZOS)
            bg = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            ox = (target_w - scaled.width) // 2
            oy = (target_h - scaled.height) // 2
            bg.paste(scaled, (ox, oy), scaled)
            scaled = bg
        else:  # stretch
            scaled = img.resize((target_w, target_h), Image.LANCZOS)
        if panel.rotation is not None:
            scaled = scaled.rotate(
                -panel.rotation,
                resample=Image.BICUBIC,
                expand=True,
            )
            scaled = ImageOps.contain(scaled, (target_w, target_h), method=Image.LANCZOS)
            bg = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            ox = (target_w - scaled.width) // 2
            oy = (target_h - scaled.height) // 2
            bg.paste(scaled, (ox, oy), scaled)
            scaled = bg
        canvas.paste(scaled, (x0, y0), scaled)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("composite failed for %s: %s", panel.image_path, exc)
        return False
    return True


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
    """Draw a page (background + panel borders + composited images) to disk.

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
    composited = 0
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
        if _composite(image, layout, (x0, y0, x1, y1)):
            composited += 1

    image.save(output_path, format="PNG")
    log.info(
        "rendered %s panels (%s composited) for %s -> %s",
        drawn,
        composited,
        page_id,
        output_path,
    )
    return PageRenderResult(
        page_id=page_id,
        output_path=output_path,
        width=page_width,
        height=page_height,
        panels_drawn=drawn,
        images_composited=composited,
    )


def load_panel_image_bytes(image_path: str | Path) -> bytes:
    """Read a panel image as PNG bytes (for API responses / thumbnails)."""

    with Image.open(image_path) as img:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


__all__ = [
    "PageRenderResult",
    "load_panel_image_bytes",
    "render_page_to_png",
]
