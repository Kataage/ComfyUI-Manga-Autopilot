"""Pytest configuration for the backend test suite.

Adds the ``src`` directory to ``sys.path`` so ``manga_autopilot`` resolves
without requiring an editable install during local development or CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
