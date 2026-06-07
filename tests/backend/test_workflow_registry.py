"""Tests for the workflow registry persistence layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manga_autopilot.services.workflow_registry import (
    WorkflowAlreadyExistsError,
    WorkflowNotFoundError,
    WorkflowRegistry,
)


def _payload(wid: str = "anime_t2i_default") -> dict:
    return {
        "workflow_id": wid,
        "name": f"Workflow {wid}",
        "type": "text_to_image",
        "file": f"workflows/{wid}_api.json",
        "bindings": {
            "positive_prompt": {"node_id": "6", "input": "text"},
            "negative_prompt": {"node_id": "7", "input": "text"},
            "seed": {"node_id": "3", "input": "seed"},
            "width": {"node_id": "5", "input": "width"},
            "height": {"node_id": "5", "input": "height"},
            "filename_prefix": {"node_id": "9", "input": "filename_prefix"},
        },
    }


def test_register_persists_payload_and_index(tmp_path: Path) -> None:
    reg = WorkflowRegistry.open(tmp_path)
    wf = reg.register(_payload())
    assert wf.workflow_id == "anime_t2i_default"
    assert (tmp_path / "workflows" / "anime_t2i_default.json").exists()
    index = json.loads((tmp_path / "workflows.json").read_text("utf-8"))
    assert index[0]["workflow_id"] == "anime_t2i_default"


def test_register_rejects_duplicates(tmp_path: Path) -> None:
    reg = WorkflowRegistry.open(tmp_path)
    reg.register(_payload())
    with pytest.raises(WorkflowAlreadyExistsError):
        reg.register(_payload())


def test_register_rejects_invalid_payload(tmp_path: Path) -> None:
    from manga_autopilot.models.workflow import WorkflowValidationError

    reg = WorkflowRegistry.open(tmp_path)
    with pytest.raises(WorkflowValidationError):
        reg.register({"workflow_id": "x"})


def test_list_and_get(tmp_path: Path) -> None:
    reg = WorkflowRegistry.open(tmp_path)
    reg.register(_payload("a"))
    reg.register(_payload("b"))
    ids = sorted(w.workflow_id for w in reg.list())
    assert ids == ["a", "b"]
    assert reg.get("a").name == "Workflow a"


def test_get_unknown_raises(tmp_path: Path) -> None:
    reg = WorkflowRegistry.open(tmp_path)
    with pytest.raises(WorkflowNotFoundError):
        reg.get("nope")


def test_update_persists_changes(tmp_path: Path) -> None:
    reg = WorkflowRegistry.open(tmp_path)
    reg.register(_payload())
    updated = _payload()
    updated["name"] = "Renamed"
    reg.update("anime_t2i_default", updated)
    on_disk = json.loads(
        (tmp_path / "workflows" / "anime_t2i_default.json").read_text("utf-8")
    )
    assert on_disk["name"] == "Renamed"
    assert reg.get("anime_t2i_default").name == "Renamed"


def test_delete_removes_files(tmp_path: Path) -> None:
    reg = WorkflowRegistry.open(tmp_path)
    reg.register(_payload())
    reg.delete("anime_t2i_default")
    assert not (tmp_path / "workflows" / "anime_t2i_default.json").exists()
    assert reg.list() == []


def test_reopen_loads_existing(tmp_path: Path) -> None:
    WorkflowRegistry.open(tmp_path).register(_payload())
    reg2 = WorkflowRegistry.open(tmp_path)
    assert "anime_t2i_default" in {w.workflow_id for w in reg2.list()}


def test_reopen_ignores_corrupt_index(tmp_path: Path) -> None:
    (tmp_path / "workflows.json").write_text("not json", encoding="utf-8")
    reg = WorkflowRegistry.open(tmp_path)
    assert reg.list() == []
