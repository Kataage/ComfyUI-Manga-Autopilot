"""Tests for the web extension loader contract."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_web_index_registers_extension() -> None:
    pkg = importlib.import_module("manga_autopilot")
    web_index = Path(pkg.WEB_DIRECTORY) / "index.js"
    source = web_index.read_text(encoding="utf-8")

    assert "app.registerExtension" in source, "index.js must register a ComfyUI extension"
    assert "comfyui.manga.autopilot" in source, "extension name must be stable"


def test_web_index_declares_sidebar_tab() -> None:
    pkg = importlib.import_module("manga_autopilot")
    web_index = Path(pkg.WEB_DIRECTORY) / "index.js"
    source = web_index.read_text(encoding="utf-8")

    assert "registerSidebarTab" in source
    assert "manga-autopilot" in source
