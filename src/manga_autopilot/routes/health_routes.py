"""Health check endpoint.

Spec reference: ``docs/comfyui_manga_autopilot_spec.md`` section 21.1, 30
Phase 0.
"""

from __future__ import annotations

import time

from aiohttp import web

from manga_autopilot import __version__

HEALTH_PATH = "/manga_autopilot/api/health"
_STARTED_AT = time.time()


async def handle_health(_request: web.Request) -> web.Response:
    """Return a small JSON document describing service liveness."""

    payload = {
        "ok": True,
        "service": "manga_autopilot",
        "version": __version__,
        "uptime_sec": round(time.time() - _STARTED_AT, 3),
    }
    return web.json_response(payload)


def register(router) -> None:  # type: ignore[no-untyped-def]
    """Register the health route on the provided router."""

    router.add_get(HEALTH_PATH, handle_health)


__all__ = ["HEALTH_PATH", "handle_health", "register"]
