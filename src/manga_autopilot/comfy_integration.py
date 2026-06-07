"""Optional ComfyUI integration glue.

When the package is imported inside a running ComfyUI process, this module is
responsible for binding our aiohttp routes onto the ``PromptServer`` singleton
and configuring the application context (storage_root, workflow registry) so
the route handlers can find their dependencies.

Outside of ComfyUI (during pytest, standalone runs, etc.) the import is a
no-op so the rest of the package stays testable.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def _resolve_app_and_routes(server_obj: object) -> tuple[object | None, object | None]:
    """Return (application, router) for a ComfyUI PromptServer-like object.

    Newer ComfyUI exposes ``.app`` (aiohttp Application). Older versions only
    have ``.routes`` (UrlDispatcher). We try both, and fall back to walking
    the attribute graph if neither matches.
    """

    app = getattr(server_obj, "app", None)
    routes = getattr(server_obj, "routes", None)
    if app is None and routes is not None and hasattr(routes, "_app"):
        app = routes._app
    return app, routes


def _default_storage_root() -> Path:
    """Pick a writable storage root when neither env nor caller set one."""

    override = os.environ.get("MANGA_AUTOPILOT_STORAGE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(tempfile.mkdtemp(prefix="manga_autopilot_")).resolve()


def attach_routes_to_prompt_server() -> bool:
    """Attach Manga Autopilot routes + context to ComfyUI's PromptServer.

    Returns ``True`` if the integration succeeded, ``False`` otherwise. This
    function never raises; failures are logged at INFO level because they are
    expected outside ComfyUI (for example during tests).
    """

    try:
        from server import PromptServer  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - exercised only inside ComfyUI
        log.info("PromptServer not available; skipping route attachment.")
        return False

    try:
        server = PromptServer.instance
    except Exception:  # pragma: no cover
        log.exception("Failed to access PromptServer.instance")
        return False

    app, routes = _resolve_app_and_routes(server)
    if app is None and routes is None:
        log.warning("PromptServer has neither .app nor .routes; skipping attachment.")
        return False

    from manga_autopilot.routes import register_all

    storage_root = _default_storage_root()
    try:
        if app is not None:
            register_all(app, storage_root=str(storage_root))
        else:
            register_all(routes, storage_root=str(storage_root))
    except Exception:  # pragma: no cover
        log.exception("Failed to register Manga Autopilot routes")
        return False

    log.info(
        "Manga Autopilot routes attached to PromptServer (storage_root=%s).",
        storage_root,
    )
    return True


__all__ = ["attach_routes_to_prompt_server"]
