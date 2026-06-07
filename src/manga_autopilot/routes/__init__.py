"""HTTP route registration for the Manga Autopilot backend.

This package houses all aiohttp route modules registered against the
ComfyUI ``PromptServer`` (when running inside ComfyUI) or a standalone
aiohttp application (for tests and development).

Each module exposes a ``register(router)`` callable that takes any object
implementing the small :class:`RouteRegistrar` protocol below.  This indirection
keeps tests independent of ComfyUI's global ``PromptServer`` singleton.

In addition to registering the HTTP routes, :func:`register_all` configures
shared application state under stable keys:

- ``manga_storage_root``         -- :class:`pathlib.Path` to on-disk storage
- ``manga_workflow_registry``    -- a :class:`WorkflowRegistry` bound to that root
- ``manga_default_storage_root`` -- the path used when none was supplied

These keys are always set when ``register_all`` runs against a web
application, so the route handlers can always find their dependencies.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from aiohttp import web

log = logging.getLogger(__name__)


@runtime_checkable
class RouteRegistrar(Protocol):
    """Protocol covering the subset of aiohttp routing we rely on."""

    def add_get(
        self, path: str, handler: Callable[[web.Request], web.StreamResponse]
    ) -> object: ...

    def add_post(
        self, path: str, handler: Callable[[web.Request], web.StreamResponse]
    ) -> object: ...


STORAGE_ROOT_KEY = "manga_storage_root"
REGISTRY_KEY = "manga_workflow_registry"
DEFAULT_STORAGE_KEY = "manga_default_storage_root"

# When this env var is set, ``register_all`` falls back to ``tempfile.mkdtemp``
# even in production.  Tests rely on this so that a stray local run can't
# pollute the user's real ComfyUI data directory.
_TEST_TEMP_ENV = "MANGA_AUTOPILOT_FORCE_TEMP_STORAGE"


def _default_storage_root() -> Path:
    """Return a durable on-disk storage root.

    Resolution order (first match wins):

    1. ``$MANGA_AUTOPILOT_FORCE_TEMP_STORAGE`` set -> ``tempfile.mkdtemp``
       (used by tests; never call this from a production startup).
    2. ``$MANGA_AUTOPILOT_STORAGE_ROOT`` env override.
    3. ``$COMFYUI_USER_DIR/manga_autopilot``.
    4. ``<cwd>/user/default/manga_autopilot`` (ComfyUI's default user dir).
    5. ``~/.manga_autopilot`` final fallback.

    The directory is created on demand so callers do not have to remember
    to ``mkdir -p`` it.
    """

    if os.environ.get(_TEST_TEMP_ENV):
        return Path(tempfile.mkdtemp(prefix="manga_autopilot_routes_")).resolve()

    override = os.environ.get("MANGA_AUTOPILOT_STORAGE_ROOT")
    if override:
        root = Path(override).expanduser().resolve()
    else:
        comfy_user = os.environ.get("COMFYUI_USER_DIR")
        if comfy_user:
            root = (Path(comfy_user) / "manga_autopilot").resolve()
        else:
            cwd_comfy_user = Path.cwd() / "user" / "default" / "manga_autopilot"
            if cwd_comfy_user.parent.parent.exists():
                root = cwd_comfy_user.resolve()
            else:
                root = (Path.home() / ".manga_autopilot").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_storage_root(app: web.Application, storage_root: str | Path | None) -> Path:
    """Resolve and persist the storage root on ``app``."""

    if storage_root is not None:
        root = Path(storage_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    elif STORAGE_ROOT_KEY in app and isinstance(app[STORAGE_ROOT_KEY], Path):
        root = app[STORAGE_ROOT_KEY]
    else:
        root = _default_storage_root()
    app[STORAGE_ROOT_KEY] = root
    app[DEFAULT_STORAGE_KEY] = root
    return root


def _ensure_registry(app: web.Application) -> None:
    """Always make sure the workflow registry is wired up on the app."""

    if REGISTRY_KEY in app and app[REGISTRY_KEY] is not None:
        return
    from manga_autopilot.services.workflow_registry import WorkflowRegistry

    app[REGISTRY_KEY] = WorkflowRegistry.open(app[STORAGE_ROOT_KEY])


def _ensure_app(router: object) -> web.Application | None:
    """Find the underlying :class:`web.Application` for ``router``.

    Accepts:

    - a :class:`web.Application` itself
    - a :class:`web.UrlDispatcher` (with a private ``_app`` back-ref)
    - an object that exposes a ``.app`` (older ComfyUI ``PromptServer``)
    - an object that exposes a ``.router`` (a router wrapping the app)
    """

    if isinstance(router, web.Application):
        return router
    if hasattr(router, "app") and isinstance(router.app, web.Application):
        return router.app
    if hasattr(router, "_app") and isinstance(router._app, web.Application):
        return router._app
    if hasattr(router, "router") and isinstance(router.router, web.Application):
        return router.router
    if hasattr(router, "router"):
        return _ensure_app(router.router)
    return None


def register_all(
    router: RouteRegistrar | web.Application,
    *,
    storage_root: str | None = None,
) -> None:
    """Register every backend route group on the supplied router.

    ``router`` may be either a thin :class:`RouteRegistrar`, an aiohttp
    :class:`web.Application`, or a :class:`web.UrlDispatcher` (as exposed by
    ComfyUI's ``PromptServer.instance.routes``).  In all cases the shared
    application context (``manga_storage_root`` /
    ``manga_workflow_registry``) is configured.
    """

    from manga_autopilot.routes import (
        autopilot_routes,
        bubble_routes,
        character_routes,
        export_routes,
        health_routes,
        workflow_routes,
    )

    app = _ensure_app(router)
    if app is not None:
        _ensure_storage_root(app, storage_root)
        _ensure_registry(app)

    health_routes.register(router)
    workflow_routes.register(router)
    bubble_routes.register(router)
    character_routes.register(router)
    autopilot_routes.register(router)
    export_routes.register(router)


__all__ = [
    "DEFAULT_STORAGE_KEY",
    "REGISTRY_KEY",
    "RouteRegistrar",
    "STORAGE_ROOT_KEY",
    "register_all",
]
