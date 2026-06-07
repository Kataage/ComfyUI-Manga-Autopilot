"""HTTP route registration for the Manga Autopilot backend.

This package houses all aiohttp route modules registered against the
ComfyUI ``PromptServer`` (when running inside ComfyUI) or a standalone
aiohttp application (for tests and development).

Each module exposes a ``register(router)`` callable that takes any object
implementing the small ``RouteRegistrar`` protocol below.  This indirection
keeps tests independent of ComfyUI's global ``PromptServer`` singleton.
"""

from __future__ import annotations

import logging
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


def _ensure_registry(app: web.Application, storage_root: str | None) -> None:
    from manga_autopilot.services.workflow_registry import WorkflowRegistry

    if storage_root:
        app["manga_workflow_registry"] = WorkflowRegistry.open(storage_root)
        return
    if "manga_workflow_registry" not in app:
        tmp = Path(tempfile.mkdtemp(prefix="manga_autopilot_routes_"))
        app["manga_workflow_registry"] = WorkflowRegistry.open(tmp)


def register_all(
    router: RouteRegistrar | web.Application,
    *,
    storage_root: str | None = None,
) -> None:
    """Register every backend route group on the supplied router.

    ``router`` may be either a thin :class:`RouteRegistrar` (in which case
    ``storage_root`` is unused) or a full :class:`aiohttp.web.Application`,
    which is the common case for tests and for ComfyUI's ``PromptServer``.
    """

    from manga_autopilot.routes import (
        autopilot_routes,
        bubble_routes,
        character_routes,
        export_routes,
        health_routes,
        workflow_routes,
    )

    if isinstance(router, web.Application):
        if storage_root is not None:
            router["manga_storage_root"] = Path(storage_root).expanduser().resolve()
        _ensure_registry(router, storage_root)

    health_routes.register(router)
    workflow_routes.register(router)
    bubble_routes.register(router)
    character_routes.register(router)
    autopilot_routes.register(router)
    export_routes.register(router)


__all__ = ["RouteRegistrar", "register_all"]
