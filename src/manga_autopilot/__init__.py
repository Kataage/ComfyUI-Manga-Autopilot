"""manga_autopilot package root.

ComfyUI loads custom_nodes by importing the top-level package and reading the
following module-level attributes:

- ``NODE_CLASS_MAPPINGS``
- ``NODE_DISPLAY_NAME_MAPPINGS``
- ``WEB_DIRECTORY``

This package intentionally exposes empty node mappings for now.  The Manga
Autopilot UI is a JavaScript ComfyUI extension served from the ``web``
directory (see ``WEB_DIRECTORY``), not a set of new ComfyUI graph nodes.
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.0.1"

NODE_CLASS_MAPPINGS: dict[str, type] = {}
"""Mapping of ComfyUI node class id -> implementation class.

Empty by design.  Manga Autopilot does not register new graph nodes; it
provides a side panel UI and an HTTP API instead.
"""

NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}
"""Mapping of ComfyUI node class id -> human readable label."""

_REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIRECTORY = str(_REPO_ROOT / "web")
"""Directory served by ComfyUI as the JavaScript extension root.

ComfyUI expects this to be a relative or absolute filesystem path. We compute
it at import time so the extension works regardless of where the
``custom_nodes/ComfyUI-Manga-Autopilot`` directory ends up on disk.
"""

def _attach_routes_quietly() -> None:
    """Best-effort hook to register routes when imported inside ComfyUI."""

    try:
        from manga_autopilot.comfy_integration import attach_routes_to_prompt_server

        attach_routes_to_prompt_server()
    except Exception:  # pragma: no cover - defensive
        # Importing during tests or non-ComfyUI environments must never raise.
        pass


_attach_routes_quietly()


__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
    "__version__",
]
