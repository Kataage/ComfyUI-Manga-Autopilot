"""Tests for the top-level ``__init__.py`` (ComfyUI custom_nodes entry point).

The root ``__init__.py`` is what ComfyUI loads when this repo is dropped
into ``custom_nodes/`` directly (no editable install).  It must:

1. Add ``<repo>/src`` to ``sys.path`` so the inner package is importable
   under its canonical name ``manga_autopilot``.
2. Re-export ``NODE_CLASS_MAPPINGS`` / ``NODE_DISPLAY_NAME_MAPPINGS`` /
   ``WEB_DIRECTORY`` from ``manga_autopilot`` (NOT from
   ``src.manga_autopilot``).
3. Not double-load the package under both ``manga_autopilot`` and
   ``src.manga_autopilot`` in ``sys.modules``.

Because actually re-importing the repo-root ``__init__.py`` from a pytest
process is invasive (it would mutate ``sys.path`` for every subsequent
test), we assert the contract statically against the source file.  The
behavioural claim — that the package ends up in ``sys.modules`` under a
single name — is verified by the existing
``test_package_skeleton.py::test_top_level_module_exposes_comfyui_symbols``
test plus a one-shot import under a subprocess in
``test_root_init_subprocess``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_INIT = REPO_ROOT / "__init__.py"


def _read() -> str:
    return ROOT_INIT.read_text(encoding="utf-8")


def test_root_init_uses_canonical_package_name() -> None:
    """The root __init__ must import via ``manga_autopilot`` (not
    ``src.manga_autopilot``) so we don't end up with the package
    registered under two names in ``sys.modules``."""

    import ast
    tree = ast.parse(_read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert not alias.name.startswith("src.manga_autopilot"), (
                    f"root __init__.py must not import from src.manga_autopilot; "
                    f"got: from {node.module} import {alias.name}"
                )
            if node.module is not None and node.module.startswith("src."):
                raise AssertionError(
                    f"root __init__.py must not import from a src.* module: {node.module}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("src.manga_autopilot"), (
                    f"root __init__.py must not import src.manga_autopilot: {alias.name}"
                )


def test_root_init_imports_comfyui_symbols_from_manga_autopilot() -> None:
    src = _read()
    # The import block must look like:
    #     from manga_autopilot import (NODE_CLASS_MAPPINGS, …)
    pattern = re.compile(
        r"from\s+manga_autopilot\s+import\s+\("
        r".*NODE_CLASS_MAPPINGS.*"
        r".*NODE_DISPLAY_NAME_MAPPINGS.*"
        r".*WEB_DIRECTORY.*"
        r"\)",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "root __init__.py must import the ComfyUI symbols from manga_autopilot; "
        f"got:\n{src}"
    )


def test_root_init_adds_src_to_sys_path() -> None:
    """The first side effect of the module must be inserting
    ``<repo>/src`` onto ``sys.path``."""

    src = _read()
    assert "sys.path" in src, "root __init__.py must touch sys.path"
    # The path is constructed relative to __file__/Path, then inserted.
    assert "Path(__file__)" in src or "Path(__file__.resolve())" in src
    assert 'parent / "src"' in src or 'parent / "src"' in src
    assert "sys.path.insert" in src


def test_root_init_subprocess() -> None:
    """End-to-end: spawn a fresh Python that does NOT have ``src/`` on
    ``sys.path`` and import the package via the root ``__init__.py``.
    The package must end up under a single ``manga_autopilot`` key in
    ``sys.modules`` (no ``src.manga_autopilot`` duplicate)."""

    bootstrap = textwrap.dedent(
        f"""
        import sys
        sys.path = [p for p in sys.path if 'ComfyUI-Manga-Autopilot' not in p]
        sys.path.insert(0, {str(REPO_ROOT)!r})
        import importlib
        mod = importlib.import_module('__init__')
        # Drop the auto-added src path to prove the root init added it.
        import manga_autopilot  # noqa
        assert 'manga_autopilot' in sys.modules
        assert 'src.manga_autopilot' not in sys.modules
        assert mod.NODE_CLASS_MAPPINGS is sys.modules['manga_autopilot'].NODE_CLASS_MAPPINGS
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
