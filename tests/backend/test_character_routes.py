"""Tests for the character HTTP routes (spec section 22)."""

from __future__ import annotations

import base64
import io

import pytest
from aiohttp import web
from PIL import Image
from pytest_aiohttp.plugin import AiohttpClient  # type: ignore

from manga_autopilot.routes import register_all
from manga_autopilot.services.character_service import (
    EXPRESSION_PRESETS,
    POSE_PRESETS,
    SHEET_VIEWS,
)


@pytest.fixture
async def storage_root(tmp_path):
    return tmp_path


@pytest.fixture
async def client(aiohttp_client: AiohttpClient, storage_root):
    app = web.Application()
    register_all(app, storage_root=str(storage_root))
    return await aiohttp_client(app)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


CHAR_PAYLOAD = {
    "id": "alice",
    "name": "Alice",
    "role": "protagonist",
    "appearance": {
        "hair_color": "silver",
        "hair_style": "long",
        "eye_color": "blue",
    },
    "outfit": {"base": "armor", "must_keep": ["silver long hair", "blue eyes"]},
    "color_palette": {"primary": "#102030", "hair": "#c0c0c0", "eyes": "#3050ff"},
    "consistency_prompt": "heroic pose",
}


async def test_full_crud(client) -> None:
    r = await client.get("/manga_autopilot/api/projects/p1/characters")
    assert r.status == 200 and await r.json() == []

    r = await client.post(
        "/manga_autopilot/api/projects/p1/characters", json=CHAR_PAYLOAD
    )
    assert r.status == 201
    data = await r.json()
    assert data["id"] == "alice"

    r = await client.get("/manga_autopilot/api/projects/p1/characters/alice")
    assert r.status == 200
    assert (await r.json())["name"] == "Alice"

    r = await client.put(
        "/manga_autopilot/api/projects/p1/characters/alice",
        json={"description": "main hero"},
    )
    assert r.status == 200
    assert (await r.json())["description"] == "main hero"

    r = await client.delete("/manga_autopilot/api/projects/p1/characters/alice")
    assert r.status == 200

    r = await client.get("/manga_autopilot/api/projects/p1/characters/alice")
    assert r.status == 404


async def test_create_rejects_duplicate(client) -> None:
    await client.post("/manga_autopilot/api/projects/p1/characters", json=CHAR_PAYLOAD)
    r = await client.post("/manga_autopilot/api/projects/p1/characters", json=CHAR_PAYLOAD)
    assert r.status == 400


async def test_create_rejects_bad_payload(client) -> None:
    bad = dict(CHAR_PAYLOAD)
    bad["id"] = "Alice Bob"
    r = await client.post("/manga_autopilot/api/projects/p1/characters", json=bad)
    assert r.status == 400


async def test_get_404(client) -> None:
    r = await client.get("/manga_autopilot/api/projects/p1/characters/ghost")
    assert r.status == 404


async def test_upload_reference(client) -> None:
    await client.post("/manga_autopilot/api/projects/p1/characters", json=CHAR_PAYLOAD)
    payload = {
        "filename": "ref.png",
        "label": "front",
        "data_base64": base64.b64encode(_png()).decode("ascii"),
    }
    r = await client.post(
        "/manga_autopilot/api/projects/p1/characters/alice/references", json=payload
    )
    assert r.status == 201
    data = await r.json()
    assert data["width"] == 16 and data["height"] == 16
    assert data["asset_ref"]["label"] == "front"


async def test_upload_rejects_bad_image(client) -> None:
    await client.post("/manga_autopilot/api/projects/p1/characters", json=CHAR_PAYLOAD)
    payload = {
        "filename": "bad.png",
        "data_base64": base64.b64encode(b"not an image").decode("ascii"),
    }
    r = await client.post(
        "/manga_autopilot/api/projects/p1/characters/alice/references", json=payload
    )
    assert r.status == 400


async def test_presets_exposed() -> None:
    assert "smile" in EXPRESSION_PRESETS
    assert "standing" in POSE_PRESETS
    assert "front" in SHEET_VIEWS
