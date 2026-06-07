"""HTTP route registration for the Manga Autopilot backend.

This package houses all aiohttp route modules registered against the
ComfyUI ``PromptServer`` (when running inside ComfyUI) or a standalone
aiohttp application (for tests and development).

Each module exposes a ``register(router)`` callable that takes any object
implementing the small ``RouteRegistrar`` protocol below.  This indirection
keeps tests independent of ComfyUI's global ``PromptServer`` singleton.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from aiohttp import web


class RouteRegistrar(Protocol):
    """Protocol covering the subset of aiohttp routing we rely on."""

    def add_get(self, path: str, handler: Callable[[web.Request], web.StreamResponse]) -> object: ...

    def add_post(self, path: str, handler: Callable[[web.Request], web.StreamResponse]) -> object: ...


def register_all(router: RouteRegistrar) -> None:
    """Register every backend route group on the supplied router."""

    from manga_autopilot.routes import health_routes

    health_routes.register(router)


__all__ = ["RouteRegistrar", "register_all"]
