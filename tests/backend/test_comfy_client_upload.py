"""Tests for ComfyClient /upload/image."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from manga_autopilot.services.comfy_client import ComfyClient


@pytest.fixture()
async def upload_server(aiohttp_server):
    seen: dict[str, dict] = {}

    async def handle_upload(request: web.Request) -> web.Response:
        reader = await request.multipart()
        fields: dict[str, object] = {}
        async for part in reader:
            name = part.name or ""
            if part.filename:
                fields["filename"] = part.filename
                fields["content_type"] = part.headers.get("Content-Type")
                fields["data"] = await part.read()
            else:
                fields[name] = await part.text()
        seen["last"] = fields
        return web.json_response(
            {
                "name": fields.get("filename"),
                "subfolder": fields.get("subfolder", ""),
                "type": fields.get("type", "input"),
            }
        )

    app = web.Application()
    app["seen"] = seen
    app.router.add_post("/upload/image", handle_upload)
    return await aiohttp_server(app)


async def _make_client(server) -> ComfyClient:
    base = f"http://{server.host}:{server.port}"
    return ComfyClient(base_url=base, timeout_sec=5)


async def test_upload_image_from_path(upload_server, tmp_path: Path) -> None:
    file_path = tmp_path / "ref.png"
    file_path.write_bytes(b"\x89PNGsample")
    async with await _make_client(upload_server) as client:
        body = await client.upload_image(file_path)
        assert body["name"] == "ref.png"
        assert body["type"] == "input"
        seen = upload_server.app["seen"]["last"]
        assert seen["filename"] == "ref.png"
        assert seen["data"] == b"\x89PNGsample"


async def test_upload_image_from_bytes(upload_server) -> None:
    async with await _make_client(upload_server) as client:
        body = await client.upload_image(
            b"BYTES", filename="from_bytes.png", subfolder="char", image_type="input"
        )
        assert body["name"] == "from_bytes.png"
        seen = upload_server.app["seen"]["last"]
        assert seen["subfolder"] == "char"
        assert seen["type"] == "input"
        assert seen["overwrite"] == "true"


async def test_upload_image_requires_filename_for_bytes() -> None:
    client = ComfyClient(base_url="http://example.com")
    with pytest.raises(ValueError):
        await client.upload_image(b"x")


async def test_upload_image_rejects_unknown_source() -> None:
    client = ComfyClient(base_url="http://example.com")
    with pytest.raises(TypeError):
        await client.upload_image(123)  # type: ignore[arg-type]
