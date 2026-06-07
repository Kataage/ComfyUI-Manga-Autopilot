"""Optional ComfyUI integration glue.

When the package is imported inside a running ComfyUI process, this module is
responsible for binding our aiohttp routes onto the ``PromptServer`` singleton.
Outside of ComfyUI (during pytest, standalone runs, etc.) the import is a
no-op so the rest of the package stays testable.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def attach_routes_to_prompt_server() -> bool:
    """Attempt to attach Manga Autopilot routes to ComfyUI's PromptServer.

    Returns ``True`` if the integration succeeded, ``False`` otherwise.
    This function never raises; failures are logged at INFO level because
    they are expected outside ComfyUI (for example during tests).
    """

    try:
        from server import PromptServer  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - exercised only inside ComfyUI
        log.info("PromptServer not available; skipping route attachment.")
        return False

    try:
        routes = PromptServer.instance.routes
    except Exception:  # pragma: no cover
        log.exception("Failed to access PromptServer.instance.routes")
        return False

    from manga_autopilot.routes import register_all

    register_all(routes)
    log.info("Manga Autopilot routes attached to PromptServer.")
    return True


__all__ = ["attach_routes_to_prompt_server"]
