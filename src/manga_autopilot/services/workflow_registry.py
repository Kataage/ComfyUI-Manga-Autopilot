"""Workflow registry persistence (spec section 12.1, 21.5)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manga_autopilot.models.workflow import (
    WorkflowDefinition,
    WorkflowValidationError,
    validate_workflow_payload,
)

INDEX_FILENAME = "workflows.json"
WORKFLOW_SUBDIR = "workflows"


@dataclass
class WorkflowRegistry:
    storage_root: Path
    workflows: dict[str, WorkflowDefinition] = field(default_factory=dict)
    index_path: Path | None = None
    workflow_dir: Path | None = None

    @classmethod
    def open(cls, storage_root: str | Path) -> WorkflowRegistry:
        root = Path(storage_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        reg = cls(storage_root=root)
        reg._initialise_paths()
        reg._load()
        return reg

    def _initialise_paths(self) -> None:
        self.index_path = self.storage_root / INDEX_FILENAME
        self.workflow_dir = self.storage_root / WORKFLOW_SUBDIR
        self.workflow_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- helpers
    def _workflow_path(self, workflow_id: str) -> Path:
        if not self.workflow_dir:
            raise RuntimeError("registry is not initialised")
        return self.workflow_dir / f"{workflow_id}.json"

    def _load(self) -> None:
        if not self.index_path or not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        if not isinstance(raw, list):
            return
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            wid = entry.get("workflow_id")
            if not isinstance(wid, str):
                continue
            payload_path = self._workflow_path(wid)
            if not payload_path.exists():
                continue
            try:
                wf = validate_workflow_payload(json.loads(payload_path.read_text("utf-8")))
            except WorkflowValidationError:
                continue
            self.workflows[wid] = wf

    # --------------------------------------------------------------- CRUD
    def register(self, payload: dict[str, Any]) -> WorkflowDefinition:
        wf = validate_workflow_payload(payload)
        if wf.workflow_id in self.workflows:
            raise WorkflowAlreadyExistsError(
                f"workflow {wf.workflow_id!r} already registered"
            )
        # Persist the full payload, including api_graph if present.
        path = self._workflow_path(wf.workflow_id)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.workflows[wf.workflow_id] = wf
        self._save_index()
        return wf

    def update(self, workflow_id: str, payload: dict[str, Any]) -> WorkflowDefinition:
        if workflow_id not in self.workflows:
            raise WorkflowNotFoundError(workflow_id)
        merged = dict(payload)
        merged["workflow_id"] = workflow_id
        wf = validate_workflow_payload(merged)
        path = self._workflow_path(workflow_id)
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.workflows[workflow_id] = wf
        self._save_index()
        return wf

    def delete(self, workflow_id: str) -> None:
        if workflow_id not in self.workflows:
            raise WorkflowNotFoundError(workflow_id)
        del self.workflows[workflow_id]
        path = self._workflow_path(workflow_id)
        if path.exists():
            path.unlink()
        self._save_index()

    def get(self, workflow_id: str) -> WorkflowDefinition:
        try:
            return self.workflows[workflow_id]
        except KeyError as exc:
            raise WorkflowNotFoundError(workflow_id) from exc

    def list(self) -> list[WorkflowDefinition]:
        return list(self.workflows.values())

    def _save_index(self) -> None:
        if not self.index_path:
            raise RuntimeError("registry is not initialised")
        entries = []
        for wid, wf in self.workflows.items():
            entries.append(
                {
                    "workflow_id": wid,
                    "name": wf.name,
                    "type": wf.type_value(),
                    "file": wf.file,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        self.index_path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class WorkflowRegistryError(Exception):
    """Base error for the workflow registry."""


class WorkflowNotFoundError(WorkflowRegistryError):
    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"workflow not found: {workflow_id}")
        self.workflow_id = workflow_id


class WorkflowAlreadyExistsError(WorkflowRegistryError):
    pass


__all__ = [
    "INDEX_FILENAME",
    "WORKFLOW_SUBDIR",
    "WorkflowRegistry",
    "WorkflowRegistryError",
    "WorkflowNotFoundError",
    "WorkflowAlreadyExistsError",
]
