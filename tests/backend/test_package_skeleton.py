"""Tests for the package skeleton expected by ComfyUI."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_top_level_module_exposes_comfyui_symbols() -> None:
    pkg = importlib.import_module("manga_autopilot")

    assert hasattr(pkg, "NODE_CLASS_MAPPINGS")
    assert hasattr(pkg, "NODE_DISPLAY_NAME_MAPPINGS")
    assert hasattr(pkg, "WEB_DIRECTORY")

    assert isinstance(pkg.NODE_CLASS_MAPPINGS, dict)
    assert isinstance(pkg.NODE_DISPLAY_NAME_MAPPINGS, dict)
    assert isinstance(pkg.WEB_DIRECTORY, str)


def test_web_directory_points_to_web_folder() -> None:
    pkg = importlib.import_module("manga_autopilot")
    web_dir = Path(pkg.WEB_DIRECTORY)
    assert web_dir.exists(), f"WEB_DIRECTORY does not exist: {web_dir}"
    assert web_dir.is_dir()
    assert (web_dir / "index.js").exists()
