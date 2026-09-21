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


def test_projects_view_renders_before_the_active_project_guard() -> None:
    """The Projects tab must not be blocked by the guard it is the cure for.

    ``activeProjectId`` starts as ``null`` and the only control that sets it
    lives inside ``createProjectsView()``.  When the guard ran first it
    covered every tab including ``projects``, so a freshly opened workspace
    showed "Set an active project id in the Projects tab to continue." on
    the Projects tab itself, with no way to set one.  Found by loading the
    extension in a real ComfyUI.
    """

    pkg = importlib.import_module("manga_autopilot")
    source = (Path(pkg.WEB_DIRECTORY) / "index.js").read_text(encoding="utf-8")

    start = source.index("const showTab = (id) => {")
    body = source[start : source.index("let activeTab =", start)]

    projects_branch = body.index('if (id === "projects")')
    guard = body.index("if (!activeProjectId)")

    assert projects_branch < guard, (
        "showTab() must render the Projects view before the activeProjectId "
        "guard; otherwise a fresh workspace can never set a project id"
    )
    assert body.count("createProjectsView()") == 1, (
        "the Projects view should be mounted from exactly one place in showTab()"
    )
