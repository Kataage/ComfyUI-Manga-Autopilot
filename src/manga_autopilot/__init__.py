"""manga_autopilot package root.

ComfyUI loads custom_nodes by importing the top-level package and reading the
following module-level attributes:

- ``NODE_CLASS_MAPPINGS``
- ``NODE_DISPLAY_NAME_MAPPINGS``
- ``WEB_DIRECTORY``

This package exposes empty node mappings by design; Manga Autopilot is a
side panel UI + HTTP API extension, not a set of new graph nodes.

**Import strategy.**  The package lives in ``src/manga_autopilot/`` (standard
``src/`` layout).  When ``pip install -e .`` has been run, ``manga_autopilot``
is already importable through the editable install.  When the repository is
dropped into ``ComfyUI/custom_nodes/ComfyUI-Manga-Autopilot/`` *without* an
editable install, ComfyUI adds the immediate ``custom_nodes/...`` directory
to ``sys.path`` but **not** the inner ``src/`` directory.  To keep every
internal ``from manga_autopilot import …`` working in both modes, the first
thing this ``__init__.py`` does is ensure ``src/`` is on ``sys.path``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


__version__ = "0.1.0-rc2"

NODE_CLASS_MAPPINGS: dict[str, type] = {}
"""Mapping of ComfyUI node class id -> implementation class.

Empty by design.  Manga Autopilot does not register new graph nodes; it
provides a side panel UI and an HTTP API instead.
"""

NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}
"""Mapping of ComfyUI node class id -> human readable label."""

_REPO_ROOT = _SRC.parent
WEB_DIRECTORY = str(_REPO_ROOT / "web")
"""Directory served by ComfyUI as the JavaScript extension root.

ComfyUI expects this to be a relative or absolute filesystem path. We compute
it at import time so the extension works regardless of where the
``custom_nodes/ComfyUI-Manga-Autopilot`` directory ends up on disk.
"""


def _default_user_data_root() -> Path:
    """Return a durable on-disk root for Manga Autopilot data.

    Resolution order:

    1. ``$MANGA_AUTOPILOT_STORAGE_ROOT`` if set (highest priority).
    2. ``$COMFYUI_USER_DIR/manga_autopilot`` if ``$COMFYUI_USER_DIR`` is set.
    3. ``ComfyUI/user/default/manga_autopilot`` relative to the ComfyUI
       process working directory.
    4. ``~/.manga_autopilot`` as a final fallback.
    """

    override = os.environ.get("MANGA_AUTOPILOT_STORAGE_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    comfy_user = os.environ.get("COMFYUI_USER_DIR")
    if comfy_user:
        return (Path(comfy_user) / "manga_autopilot").resolve()

    cwd_comfy_user = Path.cwd() / "user" / "default" / "manga_autopilot"
    if cwd_comfy_user.parent.parent.exists():
        return cwd_comfy_user.resolve()

    return (Path.home() / ".manga_autopilot").resolve()


def default_storage_root() -> Path:
    """Public alias for :func:`_default_user_data_root`."""

    return _default_user_data_root()


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
    "default_storage_root",
]
