"""HTTP routes for export (spec section 21.7)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.models.panel import PanelBorder, PanelLayout
from manga_autopilot.services.export import ExportService, PDFSize

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects/{project_id}/export"


def _service(app: Application) -> ExportService:
    storage_root: Any = app.get("manga_storage_root")
    if storage_root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    return ExportService(storage_root=storage_root)


async def _payload(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return body


def _layout_from_dict(d: dict[str, Any]) -> PanelLayout:
    side = d.get("border", {})
    rotation = d.get("rotation")
    return PanelLayout(
        panel_id=d["panel_id"],
        x=float(d.get("x", 0)),
        y=float(d.get("y", 0)),
        width=float(d.get("width", 256)),
        height=float(d.get("height", 256)),
        z_index=int(d.get("z_index", 1)),
        border=PanelBorder(
            width=float(side.get("width", 2)),
            color=side.get("color", "#000000"),
            radius=float(side.get("radius", 0)),
        ),
        margin=float(d.get("margin", 0)),
        bleed=bool(d.get("bleed", False)),
        rotation=float(rotation) if rotation is not None else None,
    )


async def export_png(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body = await _payload(request)
    pages = body.get("pages") or {}
    if not isinstance(pages, dict) or not pages:
        raise web.HTTPBadRequest(text="pages must be a non-empty object")
    page_layouts: dict[str, list[PanelLayout]] = {}
    for page_id, panels in pages.items():
        if not isinstance(panels, list):
            raise web.HTTPBadRequest(text=f"page {page_id!r} must be a list")
        page_layouts[page_id] = [_layout_from_dict(p) for p in panels]
    svc = _service(request.app)
    outputs = svc.png_pages(project_id, page_layouts)
    return web.json_response({"pages": [str(p) for p in outputs]})


async def export_webtoon(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body = await _payload(request)
    page_pngs = body.get("page_pngs") or []
    if not isinstance(page_pngs, list) or not page_pngs:
        raise web.HTTPBadRequest(text="page_pngs must be a non-empty list of paths")
    svc = _service(request.app)
    outputs = svc.webtoon(project_id, [Path(p) for p in page_pngs])
    return web.json_response({"webtoon": [str(p) for p in outputs]})


async def export_pdf(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body = await _payload(request)
    page_pngs = body.get("page_pngs") or []
    if not isinstance(page_pngs, list) or not page_pngs:
        raise web.HTTPBadRequest(text="page_pngs must be a non-empty list of paths")
    pdf_size: PDFSize = body.get("pdf_size", "A4")
    margin_mm = float(body.get("margin_mm", 10.0))
    dpi = int(body.get("dpi", 300))
    svc = _service(request.app)
    out = svc.pdf(project_id, [Path(p) for p in page_pngs], pdf_size=pdf_size, margin_mm=margin_mm, dpi=dpi)
    return web.json_response({"pdf": str(out)})


async def list_exports(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    svc = _service(request.app)
    return web.json_response({"files": [str(p) for p in svc.all_exports(project_id)]})


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_post(ROUTE_PREFIX + "/png", export_png)
    router.add_post(ROUTE_PREFIX + "/webtoon", export_webtoon)
    router.add_post(ROUTE_PREFIX + "/pdf", export_pdf)
    router.add_get(ROUTE_PREFIX + "s", list_exports)


__all__ = ["register", "ROUTE_PREFIX"]
