"""Tests that the bundled sample workflows are valid registrations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manga_autopilot.models.workflow import validate_workflow_payload

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"


@pytest.mark.parametrize("filename", sorted(WORKFLOW_DIR.glob("*_api.json")))
def test_sample_workflow_valid(filename: Path) -> None:
    payload = json.loads(filename.read_text("utf-8"))
    wf = validate_workflow_payload(payload)
    assert wf.workflow_id
    assert wf.type_value() in {
        "text_to_image",
        "image_to_image",
        "reference_to_image",
        "character_sheet",
        "upscale",
    }


def test_sample_workflow_count() -> None:
    files = list(WORKFLOW_DIR.glob("*_api.json"))
    assert len(files) >= 5, f"expected at least 5 sample workflows, got {len(files)}"
