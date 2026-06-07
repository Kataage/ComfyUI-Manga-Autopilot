"""Export service (spec sections 20.1-20.3, 21.7, 22 Export Center).

Provides:

- :class:`ExportService` - top-level orchestrator (png / webtoon / pdf / zip)
- :class:`WebtoonRenderer` - stitch panels vertically with margin rules
- :class:`WebtoonSlicer` - split tall webtoon into slices
- :class:`PDFRenderer` - assemble PNG pages into a PDF (with PIL)
- :class:`ProjectBundler` - zip a project directory; :class:`ProjectImporter`
- HTTP routes in :mod:`manga_autopilot.routes.export_routes`
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from PIL import Image

from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.services.page_renderer import render_page_to_png
from manga_autopilot.storage.paths import (
    ensure_project_paths,
    project_paths,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------- PNG export
def export_page_png(
    project_root: str | Path,
    page_id: str,
    panels: Sequence[PanelLayout],
    *,
    page_width: int = 1200,
    page_height: int = 1600,
    background: str = "#ffffff",
) -> Path:
    """Render a single page to ``exports/pages/page_NNNN.png``."""

    project_root = Path(project_root)
    out_dir = project_root / "exports" / "pages"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = render_page_to_png(
        page_id,
        panels,
        output_dir=out_dir,
        page_width=page_width,
        page_height=page_height,
        background=background,
    )
    return result.output_path


# --------------------------------------------------------- Webtoon
WEBTOON_WIDTH = 1080
WEBTOON_MAX_HEIGHT = 12_000


@dataclass
class WebtoonRenderResult:
    output_path: Path
    width: int
    height: int
    panels_stitched: int
    page_ids: list[str]


@dataclass
class WebtoonRenderer:
    """Stitch pages vertically into a single webtoon PNG (spec 20.2)."""

    width: int = WEBTOON_WIDTH
    panel_margin: int = 16
    scene_break_margin: int = 64
    page_gap: int = 8
    background: tuple[int, int, int] = (255, 255, 255)

    def stitch(
        self,
        page_renders: Sequence[tuple[str, Image.Image]],
        *,
        scene_breaks_after: Iterable[str] | None = None,
    ) -> WebtoonRenderResult:
        if not page_renders:
            raise ValueError("page_renders is empty")
        scene_breaks = set(scene_breaks_after or [])
        scale = self.width / max(page_renders[0][1].width, 1)
        scaled_pages: list[tuple[str, Image.Image]] = []
        for pid, img in page_renders:
            new_h = int(img.height * scale)
            scaled_pages.append((pid, img.resize((self.width, new_h), Image.LANCZOS)))

        total_height = sum(img.height for _, img in scaled_pages) + self.page_gap * (len(scaled_pages) - 1)
        for pid, _ in scaled_pages:
            if pid in scene_breaks:
                total_height += self.scene_break_margin - self.page_gap
        canvas = Image.new("RGB", (self.width, total_height), self.background)
        y = 0
        page_ids: list[str] = []
        for i, (pid, img) in enumerate(scaled_pages):
            if i > 0:
                y += self.page_gap
            if pid in scene_breaks:
                y += self.scene_break_margin
            canvas.paste(img, (0, y))
            page_ids.append(pid)
            y += img.height
        return WebtoonRenderResult(
            output_path=Path(),
            width=self.width,
            height=total_height,
            panels_stitched=len(scaled_pages),
            page_ids=page_ids,
        )


@dataclass
class WebtoonSlicer:
    """Split an oversized webtoon into max-height slices (spec 20.2)."""

    max_height: int = WEBTOON_MAX_HEIGHT
    output_prefix: str = "webtoon"

    def slice(
        self,
        image: Image.Image,
        *,
        output_dir: str | Path,
    ) -> list[Path]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        if image.height <= self.max_height:
            path = output_dir / f"{self.output_prefix}_001.png"
            image.save(path, format="PNG")
            return [path]
        paths: list[Path] = []
        idx = 1
        y = 0
        while y < image.height:
            bottom = min(y + self.max_height, image.height)
            slice_img = image.crop((0, y, image.width, bottom))
            path = output_dir / f"{self.output_prefix}_{idx:03d}.png"
            slice_img.save(path, format="PNG")
            paths.append(path)
            y = bottom
            idx += 1
        return paths


# --------------------------------------------------------- PDF
PDFSize = Literal["A4", "B5", "Kindle", "custom"]


@dataclass
class PDFRenderResult:
    output_path: Path
    pages: int
    size: str


@dataclass
class PDFRenderer:
    """Assemble PNG pages into a PDF (spec 20.3)."""

    pdf_size: PDFSize = "A4"
    margin_mm: float = 10.0
    dpi: int = 300
    include_cover: bool = False
    reading_direction: Literal["left_to_right", "right_to_left"] = "right_to_left"

    # Standard sizes in mm
    SIZE_MM: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "A4": (210.0, 297.0),
            "B5": (176.0, 250.0),
            "Kindle": (107.0, 174.0),
        }
    )

    def _page_size_mm(self) -> tuple[float, float]:
        if self.pdf_size == "custom":
            return (210.0, 297.0)  # default
        return self.SIZE_MM.get(self.pdf_size, (210.0, 297.0))

    def assemble(
        self,
        page_paths: Sequence[Path],
        *,
        output_path: str | Path,
    ) -> PDFRenderResult:
        if not page_paths:
            raise ValueError("page_paths is empty")
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        page_w_mm, page_h_mm = self._page_size_mm()
        # Use a uniform canvas per page sized to the spec output, then paste
        # each PNG centered with the configured margin.  We hand the canvas
        # list to PIL to build the PDF.
        canvas_w_px = int((page_w_mm - 2 * self.margin_mm) * self.dpi / 25.4)
        canvas_h_px = int((page_h_mm - 2 * self.margin_mm) * self.dpi / 25.4)
        if canvas_w_px <= 0 or canvas_h_px <= 0:
            raise ValueError("margin leaves no room for image content")
        pages: list[Image.Image] = []
        for src in page_paths:
            with Image.open(src) as img:
                img = img.convert("RGB")
                img.thumbnail((canvas_w_px, canvas_h_px), Image.LANCZOS)
                canvas = Image.new("RGB", (canvas_w_px, canvas_h_px), (255, 255, 255))
                x = (canvas_w_px - img.width) // 2
                y = (canvas_h_px - img.height) // 2
                canvas.paste(img, (x, y))
                pages.append(canvas)
        if self.reading_direction == "right_to_left":
            pages = list(reversed(pages))
        first, rest = pages[0], pages[1:]
        first.save(out_path, format="PDF", save_all=True, append_images=rest)
        return PDFRenderResult(output_path=out_path, pages=len(pages), size=self.pdf_size)


# --------------------------------------------------------- Bundler
@dataclass
class ProjectBundler:
    """Zip a project directory (spec 9.1 export)."""

    storage_root: Path

    def bundle(self, project_id: str, output_path: str | Path) -> Path:
        project = project_paths(self.storage_root, project_id)
        if not project.root.exists():
            raise FileNotFoundError(project.root)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(project.root.rglob("*")):
                if path.is_file():
                    zf.write(path, arcname=str(path.relative_to(project.root)))
        return out


@dataclass
class ProjectImporter:
    """Import a previously-bundled project zip back into storage.

    The importer is **safe against Zip Slip** attacks (CVE-2018-1002200 family)
    and against absolute / parent-traversal paths inside the archive.  Every
    entry is resolved relative to the project root and rejected if it escapes
    that root.
    """

    storage_root: Path

    def _safe_target(self, target_root: Path, name: str) -> Path:
        """Validate ``name`` resolves inside ``target_root`` and return the safe path."""

        # Normalise slashes; reject absolute paths outright.
        normalised = name.replace("\\", "/")
        if normalised.startswith("/") or normalised.startswith("\\"):
            raise ValueError(f"zip entry has an absolute path: {name!r}")
        # ``PurePosixPath`` treats ``..`` lexically so it can't escape the root.
        candidate = PurePosixPath(normalised)
        if any(part == ".." for part in candidate.parts):
            raise ValueError(f"zip entry escapes the project root: {name!r}")
        dest = (target_root / candidate).resolve()
        target_resolved = target_root.resolve()
        try:
            dest.relative_to(target_resolved)
        except ValueError as exc:
            raise ValueError(
                f"zip entry {name!r} escapes the project root after resolution"
            ) from exc
        return dest

    def import_zip(self, zip_path: str | Path, project_id: str | None = None) -> Path:
        zip_path = Path(zip_path)
        if not zip_path.exists():
            raise FileNotFoundError(zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if not names:
                raise ValueError("zip is empty")
            inferred_id = project_id or zip_path.stem
            target = ensure_project_paths(self.storage_root, inferred_id)
            for entry in zf.infolist():
                safe_name = self._safe_target(target.root, entry.filename)
                if entry.is_dir():
                    safe_name.mkdir(parents=True, exist_ok=True)
                    continue
                safe_name.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, safe_name.open("wb") as out:
                    while True:
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
        return target.root


# --------------------------------------------------------- Service
@dataclass
class ExportResult:
    pages: list[Path] = field(default_factory=list)
    webtoon: list[Path] = field(default_factory=list)
    pdf: Path | None = None
    zip: Path | None = None


@dataclass
class ExportService:
    """Top-level export orchestrator."""

    storage_root: Path

    def png_pages(
        self,
        project_id: str,
        pages: Mapping[str, Sequence[PanelLayout]],
    ) -> list[Path]:
        paths = ensure_project_paths(self.storage_root, project_id)
        out: list[Path] = []
        for page_id, panels in pages.items():
            out.append(export_page_png(paths.root, page_id, panels))
        return out

    def webtoon(
        self,
        project_id: str,
        page_pngs: Sequence[Path],
        *,
        max_height: int = WEBTOON_MAX_HEIGHT,
        width: int = WEBTOON_WIDTH,
    ) -> list[Path]:
        paths = ensure_project_paths(self.storage_root, project_id)
        out_dir = paths.export("webtoon")
        # Stitch the supplied PNGs into one webtoon
        renderer = WebtoonRenderer(width=width)
        page_renders: list[tuple[str, Image.Image]] = []
        for i, p in enumerate(page_pngs, start=1):
            page_renders.append((f"page_{i:04d}", Image.open(p)))
        result = renderer.stitch(page_renders)
        result.output_path = out_dir / "webtoon_full.png"
        if not result.output_path.parent.exists():
            result.output_path.parent.mkdir(parents=True, exist_ok=True)
        result_path = result.output_path
        # Re-stitch with output saving
        canvas_h = result.height
        canvas = Image.new("RGB", (result.width, canvas_h), (255, 255, 255))
        y = 0
        for img in (i for _, i in page_renders):
            img = img.resize((result.width, int(img.height * result.width / img.width)), Image.LANCZOS)
            canvas.paste(img, (0, y))
            y += img.height + renderer.page_gap
        canvas.save(result_path, format="PNG")
        # Slice if needed
        slices = WebtoonSlicer(max_height=max_height).slice(canvas, output_dir=out_dir)
        return slices

    def pdf(
        self,
        project_id: str,
        page_pngs: Sequence[Path],
        *,
        pdf_size: PDFSize = "A4",
        margin_mm: float = 10.0,
        dpi: int = 300,
    ) -> Path:
        paths = ensure_project_paths(self.storage_root, project_id)
        out_path = paths.export("pdf") / "manga.pdf"
        result = PDFRenderer(pdf_size=pdf_size, margin_mm=margin_mm, dpi=dpi).assemble(
            page_pngs, output_path=out_path
        )
        return result.output_path

    def zip(self, project_id: str, output_path: str | Path) -> Path:
        return ProjectBundler(self.storage_root).bundle(project_id, output_path)

    def import_zip(self, zip_path: str | Path, project_id: str | None = None) -> Path:
        return ProjectImporter(self.storage_root).import_zip(zip_path, project_id)

    def all_exports(self, project_id: str) -> list[Path]:
        paths = project_paths(self.storage_root, project_id)
        files: list[Path] = []
        for sub in ("pages", "webtoon", "pdf"):
            d = paths.export(sub)
            if d.exists():
                files.extend(sorted(p for p in d.rglob("*") if p.is_file()))
        return files

    def resolve_page_pngs(
        self,
        project_id: str,
        page_pngs: Sequence[str | Path],
        *,
        allow_asset_images: bool = False,
    ) -> list[Path]:
        """Validate that every page PNG lives inside the project's export tree.

        By default, only paths that resolve to a file directly inside
        ``{storage_root}/projects/{project_id}/exports/pages/`` are accepted.
        Absolute paths and paths that escape the project root raise
        :class:`ValueError`.

        The legacy code also accepted ``assets/`` (panel images).  That
        behaviour is preserved as an opt-in through ``allow_asset_images``;
        callers (typically the export HTTP routes) need to set it explicitly
        to surface panel-level images for, e.g., a custom webtoon build.
        """

        paths = project_paths(self.storage_root, project_id)
        allowed_roots = [paths.export("pages").resolve()]
        if allow_asset_images:
            allowed_roots.append(paths.assets.resolve())
        resolved: list[Path] = []
        for raw in page_pngs:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                raise ValueError(
                    f"page_pngs entries must be absolute paths; got {p!r}"
                )
            abs_path = p.resolve()
            if not abs_path.is_file():
                raise ValueError(f"page_pngs entry does not exist: {abs_path}")
            inside = any(
                _is_within(abs_path, root) for root in allowed_roots
            )
            if not inside:
                raise ValueError(
                    f"page_pngs entry is outside the project storage tree: {abs_path}"
                )
            resolved.append(abs_path)
        return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "ExportResult",
    "ExportService",
    "PDFRenderResult",
    "PDFRenderer",
    "PDFSize",
    "ProjectBundler",
    "ProjectImporter",
    "WEBTOON_MAX_HEIGHT",
    "WEBTOON_WIDTH",
    "WebtoonRenderResult",
    "WebtoonRenderer",
    "WebtoonSlicer",
    "export_page_png",
]
