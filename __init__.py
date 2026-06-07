"""ComfyUI Manga Autopilot custom node entry point.

This module is auto-loaded by ComfyUI from ``ComfyUI/custom_nodes/``.
It re-exports the module-level symbols ComfyUI looks for and ensures the
JavaScript extension under ``web/`` is registered via ``WEB_DIRECTORY``.

Refer to ``docs/comfyui_manga_autopilot_spec.md`` for the full specification.
"""

from __future__ import annotations

from src.manga_autopilot import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    WEB_DIRECTORY,
)

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
