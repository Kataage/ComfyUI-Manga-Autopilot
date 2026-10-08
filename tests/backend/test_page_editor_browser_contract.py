"""Execute native Node browser-contract tests through the existing pytest CI matrix."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BROWSER_SUITE = ROOT / "tests" / "frontend" / "page_editor_contract.mjs"


def test_page_editor_browser_request_and_interaction_contract() -> None:
    """Exercises actual browser JS, not a Python reimplementation of the API."""
    assert BROWSER_SUITE.is_file(), "the browser request contract test must exist"
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to run browser-side contract tests")
    result = subprocess.run(
        [node, "--test", str(BROWSER_SUITE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        "Browser-side Page Editor contract tests failed:\n"
        + result.stdout + "\n" + result.stderr
    )
