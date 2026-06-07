"""Smoke test for the CI workflow file.

We don't run the workflow in tests (that would require GitHub Actions), but
we do assert that the file exists, parses as valid YAML, and declares the
matrix entries we promise in the issue.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_file_exists() -> None:
    assert WORKFLOW.exists(), f"CI workflow missing: {WORKFLOW}"


def test_ci_workflow_parses() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("name") == "ci"
    # PyYAML parses the unquoted YAML key `on:` as the boolean True.
    on = data.get("on") or data.get(True)
    assert on is not None
    assert "push" in on
    assert "pull_request" in on


def test_ci_workflow_has_python_matrix() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    matrix_versions = (
        data["jobs"]["backend"]["strategy"]["matrix"]["python-version"]
    )
    assert "3.10" in matrix_versions
    assert "3.11" in matrix_versions


def test_ci_workflow_runs_pytest_and_ruff() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["backend"]["steps"]
    flat = "\n".join(str(s.get("run", "")) for s in steps)
    assert "pytest" in flat
    assert "ruff" in flat
