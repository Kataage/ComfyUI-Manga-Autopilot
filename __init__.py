"""ComfyUI Manga Autopilot custom node entry point.

This module is auto-loaded by ComfyUI from ``ComfyUI/custom_nodes/``.
It re-exports the module-level symbols ComfyUI looks for and ensures the
JavaScript extension under ``web/`` is registered via ``WEB_DIRECTORY``.

Import strategy
----------------
The actual package lives in ``src/manga_autopilot/`` (standard ``src/``
layout).  When ``pip install -e .`` has been run, ``manga_autopilot`` is
already importable through the editable install.  When this repository is
dropped into ``ComfyUI/custom_nodes/ComfyUI-Manga-Autopilot/`` *without*
an editable install, ComfyUI adds the immediate ``custom_nodes/...``
directory to ``sys.path`` but **not** the inner ``src/`` directory.  To
make ``from manga_autopilot import …`` work in both modes, this top-level
``__init__.py`` first inserts ``<repo>/src`` onto ``sys.path`` and then
imports the package under its canonical ``manga_autopilot`` name.

We deliberately do **not** import via ``src.manga_autopilot`` anywhere
in the project: doing so would create a second copy of the package in
``sys.modules`` (one keyed under ``manga_autopilot`` and one under
``src.manga_autopilot``), and any class identity check across the two
would silently fail.

Refer to ``docs/comfyui_manga_autopilot_spec.md`` for the full specification.
"""

from __future__ import annotations

import sys
from pathlib import Path

# <repo>/src — the directory that actually contains the package.
_SRC = Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Import under the canonical package name; the ``src/`` layout is just a
# build-system convention, not a Python namespace.
from manga_autopilot import (  # noqa: E402
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    WEB_DIRECTORY,
    __version__,
)

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
    "__version__",
]
