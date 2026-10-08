"""Execute Export Center's native JS browser/request contract in pytest CI."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "frontend" / "export_center_contract.mjs"


def test_work_export_center_browser_contract() -> None:
    assert SUITE.is_file(), "Export Center browser-contract tests must exist"
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser contract test")
    result = subprocess.run(
        [node, "--test", str(SUITE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        "Work Export Center browser tests failed:\n"
        + result.stdout + "\n" + result.stderr
    )
