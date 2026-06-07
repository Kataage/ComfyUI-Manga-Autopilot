"""Tests for the ComfyUI integration: routes, storage_root, and registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from manga_autopilot.comfy_integration import (
    _resolve_app_and_routes,
    attach_routes_to_prompt_server,
)
from manga_autopilot.routes import (
    DEFAULT_STORAGE_KEY,
    REGISTRY_KEY,
    STORAGE_ROOT_KEY,
    register_all,
)
from manga_autopilot.services.workflow_registry import WorkflowRegistry


class _FakePromptServer:
    """Mimic the ComfyUI ``PromptServer.instance`` surface used by us."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.routes = self.app.router


@pytest.fixture(autouse=True)
def _clear_storage_env(monkeypatch):
    monkeypatch.delenv("MANGA_AUTOPILOT_STORAGE_ROOT", raising=False)


def test_resolve_app_and_routes_handles_promptserver():
    server = _FakePromptServer()
    app, routes = _resolve_app_and_routes(server)
    assert app is server.app
    assert routes is server.app.router


def test_default_storage_root_uses_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MANGA_AUTOPILOT_STORAGE_ROOT", str(tmp_path))
    monkeypatch.delenv("MANGA_AUTOPILOT_FORCE_TEMP_STORAGE", raising=False)
    from manga_autopilot.routes import _default_storage_root
    root = _default_storage_root()
    assert root == tmp_path.resolve()


def test_default_storage_root_persists_across_calls(tmp_path: Path, monkeypatch):
    """The default storage root must be a durable on-disk path, not a tempdir."""

    target = tmp_path / "durable_manga"
    monkeypatch.setenv("MANGA_AUTOPILOT_STORAGE_ROOT", str(target))
    monkeypatch.delenv("MANGA_AUTOPILOT_FORCE_TEMP_STORAGE", raising=False)
    from manga_autopilot.routes import _default_storage_root
    root1 = _default_storage_root()
    root2 = _default_storage_root()
    assert root1 == root2
    assert "tmp" not in str(root1).lower() or root1 == target.resolve()
    assert root1.exists()


def test_default_storage_root_force_temp_uses_tempdir(monkeypatch):
    monkeypatch.setenv("MANGA_AUTOPILOT_FORCE_TEMP_STORAGE", "1")
    from manga_autopilot.routes import _default_storage_root
    root = _default_storage_root()
    import tempfile
    assert str(root).startswith(tempfile.gettempdir())


def test_package_self_registers_src_path():
    """Importing the package should add ``src/`` to ``sys.path`` so that
    ``from manga_autopilot import …`` works even from a custom_nodes direct
    placement (no editable install)."""

    import sys
    from pathlib import Path

    import manga_autopilot  # noqa: F401
    import manga_autopilot.routes  # noqa: F401
    pkg_file = manga_autopilot.routes.__file__
    # pkg_file = .../src/manga_autopilot/routes/__init__.py
    # src  = .../src
    src = Path(pkg_file).resolve().parent.parent.parent
    # Whether pytest pre-added `src/` or not, the package's __init__.py
    # must keep it present.  If pytest pre-added it, the package should
    # have left it alone (no duplicate).  Either way ``src`` is on
    # ``sys.path`` so the package importable without an editable install.
    assert str(src) in sys.path
    # And the package is wired up: it has the comfyui integration glue
    # registered.
    assert hasattr(manga_autopilot, "default_storage_root")
    assert hasattr(manga_autopilot, "WEB_DIRECTORY")


def test_attach_routes_to_prompt_server_uses_fake(monkeypatch):
    """When PromptServer.instance is a fake, attach should wire everything up."""

    import sys
    import types

    server = _FakePromptServer()
    fake_mod = types.ModuleType("server")
    fake_mod.PromptServer = type("PromptServer", (), {"instance": server})
    monkeypatch.setitem(sys.modules, "server", fake_mod)

    ok = attach_routes_to_prompt_server()
    assert ok is True

    app = server.app
    assert STORAGE_ROOT_KEY in app
    assert REGISTRY_KEY in app
    assert isinstance(app[REGISTRY_KEY], WorkflowRegistry)
    assert app[STORAGE_ROOT_KEY] == app[DEFAULT_STORAGE_KEY]


def test_register_all_against_bare_router_doesnt_500():
    """register_all should tolerate being called with a UrlDispatcher (no Application)."""

    app = web.Application()
    router = app.router  # UrlDispatcher (no add_get, add_post; only a private _app backref)
    register_all(router)
    # Because no Application was found, storage_root is not set; but no exception either.
    # Subsequent explicit register_all(app) still works.
    register_all(app, storage_root="/tmp/manga-noop")


async def test_character_routes_do_not_500_after_comfyui_startup(aiohttp_client, tmp_path: Path):
    """Spec issue #1: after ComfyUI boots, /characters, /export, /workflows must not 500."""

    app = web.Application()
    register_all(app, storage_root=str(tmp_path))

    client = await aiohttp_client(app)

    # Workflow endpoints
    r = await client.get("/manga_autopilot/api/workflows")
    assert r.status == 200
    r = await client.post(
        "/manga_autopilot/api/workflows",
        json={
            "workflow_id": "wf_x",
            "name": "X",
            "type": "text_to_image",
            "file": "wf_x.json",
            "bindings": {
                "positive_prompt": {"node_id": "6", "input": "text"},
                "negative_prompt": {"node_id": "7", "input": "text"},
                "seed": {"node_id": "3", "input": "seed"},
                "width": {"node_id": "5", "input": "width"},
                "height": {"node_id": "5", "input": "height"},
                "filename_prefix": {"node_id": "9", "input": "filename_prefix"},
            },
        },
    )
    assert r.status == 201

    # Character endpoints
    r = await client.get("/manga_autopilot/api/projects/proj/characters")
    assert r.status == 200
    assert await r.json() == []

    # Bubble endpoints
    r = await client.get("/manga_autopilot/api/projects/proj/bubbles")
    assert r.status == 200

    # Export endpoints
    r = await client.get("/manga_autopilot/api/projects/proj/exports")
    assert r.status == 200

    # Health
    r = await client.get("/manga_autopilot/api/health")
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["service"] == "manga_autopilot"


async def test_character_routes_no_storage_root_returns_500(aiohttp_client):
    app = web.Application()
    # Note: we explicitly do NOT call register_all() here, so storage_root
    # is never set. The route should surface 500 instead of crashing.
    from manga_autopilot.routes import character_routes, health_routes, workflow_routes

    health_routes.register(app)
    workflow_routes.register(app)
    character_routes.register(app)
    client = await aiohttp_client(app)

    r = await client.get("/manga_autopilot/api/projects/p/characters")
    assert r.status == 500
