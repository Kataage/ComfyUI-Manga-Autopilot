"""Tests for the export service (spec sections 20.1-20.3, 21.7)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from aiohttp import web
from PIL import Image
from pytest_aiohttp.plugin import AiohttpClient  # type: ignore

from manga_autopilot.models.panel import PanelBorder, PanelLayout
from manga_autopilot.routes import register_all
from manga_autopilot.services.export import (
    WEBTOON_MAX_HEIGHT,
    WEBTOON_WIDTH,
    PDFRenderer,
    ProjectBundler,
    ProjectImporter,
    WebtoonRenderer,
    WebtoonSlicer,
    export_page_png,
)


def _layout(panel_id: str, x: int, y: int, w: int = 256, h: int = 256) -> PanelLayout:
    return PanelLayout(
        panel_id=panel_id,
        x=float(x),
        y=float(y),
        width=float(w),
        height=float(h),
        z_index=1,
        border=PanelBorder(width=2.0, color="#000000", radius=0.0),
        margin=0.0,
        bleed=False,
    )


def _png(w: int, h: int, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------- PNG
def test_export_page_png_writes_file(tmp_path: Path) -> None:
    project_root = tmp_path / "projects" / "p1"
    project_root.mkdir(parents=True)
    (project_root / "assets").mkdir()
    (project_root / "exports").mkdir()
    (project_root / "exports" / "pages").mkdir()
    out = export_page_png(project_root, "page_1", [_layout("p1", 16, 16, 200, 200)])
    assert out.exists()
    img = Image.open(out)
    assert img.size == (1200, 1600)


# --------------------------------------------------------------- Webtoon
def test_webtoon_stitch_resizes_to_width() -> None:
    img1 = Image.new("RGB", (600, 800), (255, 0, 0))
    img2 = Image.new("RGB", (600, 600), (0, 0, 255))
    res = WebtoonRenderer(width=720, page_gap=4).stitch([("page_1", img1), ("page_2", img2)])
    assert res.width == 720
    assert res.panels_stitched == 2
    assert res.height > 0


def test_webtoon_scene_break_adds_margin() -> None:
    img1 = Image.new("RGB", (100, 100), (255, 0, 0))
    img2 = Image.new("RGB", (100, 100), (0, 0, 255))
    plain = WebtoonRenderer(width=100, page_gap=4, scene_break_margin=64).stitch(
        [("a", img1), ("b", img2)]
    )
    with_break = WebtoonRenderer(width=100, page_gap=4, scene_break_margin=64).stitch(
        [("a", img1), ("b", img2)],
        scene_breaks_after={"a"},
    )
    assert with_break.height > plain.height


def test_webtoon_slicer_no_op_when_under_max() -> None:
    img = Image.new("RGB", (200, 1000), (255, 255, 255))
    out = WebtoonSlicer(max_height=2000).slice(img, output_dir=Path("/tmp"))
    assert len(out) == 1


def test_webtoon_slicer_splits() -> None:
    img = Image.new("RGB", (200, 5000), (255, 255, 255))
    out = WebtoonSlicer(max_height=2000, output_prefix="x").slice(img, output_dir=Path("/tmp"))
    assert len(out) == 3
    for p in out:
        assert p.name.startswith("x_")
        assert p.name.endswith(".png")


def test_webtoon_stitch_empty_raises() -> None:
    with pytest.raises(ValueError):
        WebtoonRenderer().stitch([])


# --------------------------------------------------------------- PDF
def test_pdf_renderer_assembles(tmp_path: Path) -> None:
    p1 = tmp_path / "p1.png"
    p2 = tmp_path / "p2.png"
    Image.new("RGB", (200, 300), (255, 0, 0)).save(p1)
    Image.new("RGB", (200, 300), (0, 0, 255)).save(p2)
    out = tmp_path / "manga.pdf"
    res = PDFRenderer(pdf_size="A4", margin_mm=10, dpi=72).assemble([p1, p2], output_path=out)
    assert out.exists() and out.stat().st_size > 0
    assert res.pages == 2


def test_pdf_renderer_empty_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PDFRenderer().assemble([], output_path=tmp_path / "manga.pdf")


def test_pdf_renderer_margin_too_large() -> None:
    with pytest.raises(ValueError):
        PDFRenderer(pdf_size="Kindle", margin_mm=300, dpi=72).assemble(
            [Path("/tmp/a.png")], output_path=Path("/tmp/out.pdf")
        )


# --------------------------------------------------------------- Bundler
def test_bundler_round_trip(tmp_path: Path) -> None:
    project_root = tmp_path / "projects" / "p1"
    project_root.mkdir(parents=True)
    (project_root / "files").mkdir()
    (project_root / "project.json").write_text(json.dumps({"id": "p1"}))
    (project_root / "files" / "a.txt").write_text("hello")
    zip_path = tmp_path / "bundle.zip"
    ProjectBundler(tmp_path).bundle("p1", zip_path)
    assert zip_path.exists()
    # Verify contents
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
    assert "project.json" in names
    assert "files/a.txt" in names
    # Round trip via importer
    extract_root = tmp_path / "imported"
    extract_root.mkdir()
    new_root = ProjectImporter(extract_root).import_zip(zip_path, "p1")
    assert (new_root / "project.json").exists()
    assert (new_root / "files" / "a.txt").read_text() == "hello"


def test_bundler_missing_project(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ProjectBundler(tmp_path).bundle("ghost", tmp_path / "out.zip")


def test_importer_missing_zip(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ProjectImporter(tmp_path).import_zip(tmp_path / "missing.zip")


# --------------------------------------------------------------- Service
def test_export_service_png(tmp_path: Path) -> None:
    from manga_autopilot.services.export import ExportService

    svc = ExportService(storage_root=tmp_path)
    out = svc.png_pages(
        "p1",
        {"page_1": [_layout("p1", 16, 16, 200, 200)]},
    )
    assert out and out[0].exists()


def test_export_service_pdf(tmp_path: Path) -> None:
    from manga_autopilot.services.export import ExportService

    svc = ExportService(storage_root=tmp_path)
    page = export_page_png(
        tmp_path / "projects" / "p1",
        "page_1",
        [_layout("p1", 16, 16, 200, 200)],
    )
    pdf = svc.pdf("p1", [page])
    assert pdf.exists()


def test_export_service_all_exports(tmp_path: Path) -> None:
    from manga_autopilot.services.export import ExportService

    svc = ExportService(storage_root=tmp_path)
    svc.png_pages("p1", {"page_1": [_layout("p1", 16, 16, 200, 200)]})
    files = svc.all_exports("p1")
    assert any(str(f).endswith("page_0001.png") for f in files)


def test_webtoon_constants() -> None:
    assert WEBTOON_WIDTH == 1080
    assert WEBTOON_MAX_HEIGHT == 12_000


# --------------------------------------------------------------- Routes
@pytest.fixture
async def storage_root(tmp_path):
    return tmp_path


@pytest.fixture
async def client(aiohttp_client: AiohttpClient, storage_root):
    app = web.Application()
    register_all(app, storage_root=str(storage_root))
    return await aiohttp_client(app)


async def test_export_routes_png(client) -> None:
    body = {"pages": {"page_1": [{"panel_id": "p1", "x": 16, "y": 16, "width": 200, "height": 200}]}}
    r = await client.post("/manga_autopilot/api/projects/p1/export/png", json=body)
    assert r.status == 200
    data = await r.json()
    assert data["pages"][0].endswith("page_0001.png")


async def test_export_routes_list(client) -> None:
    r = await client.get("/manga_autopilot/api/projects/p1/exports")
    assert r.status == 200


async def test_export_routes_pdf(client) -> None:
    body = {"page_pngs": []}
    r = await client.post("/manga_autopilot/api/projects/p1/export/pdf", json=body)
    assert r.status == 400
