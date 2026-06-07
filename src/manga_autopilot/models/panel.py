"""Panel layout + per-panel persistence (spec sections 14.5, 15.5, 39.B)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from manga_autopilot.models.page import PanelPlan

PANEL_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class PanelBorder(BaseModel):
    width: float = Field(default=2.0, ge=0.0, le=64.0)
    color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    radius: float = Field(default=0.0, ge=0.0, le=128.0)


class PanelLayout(BaseModel):
    panel_id: str = Field(min_length=1, max_length=64)
    x: float = 0.0
    y: float = 0.0
    width: float = Field(default=512.0, gt=0.0)
    height: float = Field(default=512.0, gt=0.0)
    z_index: int = 0
    border: PanelBorder = Field(default_factory=PanelBorder)
    margin: float = Field(default=0.0, ge=0.0, le=512.0)
    bleed: bool = False
    rotation: float | None = Field(default=None, ge=-180.0, le=180.0)

    @field_validator("panel_id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not PANEL_ID_RE.fullmatch(value):
            raise ValueError(
                "panel_id must match ^[A-Za-z0-9_-]{1,64}$"
            )
        return value


class PanelRecord(BaseModel):
    panel_id: str = Field(min_length=1, max_length=64)
    page_number: int = Field(ge=1)
    plan: PanelPlan
    layout: PanelLayout | None = None
    status: str = Field(default="draft")
    workflow_id: str | None = None
    prompt_id: str | None = None
    image_path: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4096)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str) -> str:
        allowed = {
            "draft",
            "queued",
            "running",
            "generated",
            "approved",
            "rejected",
            "failed",
        }
        if value not in allowed:
            raise ValueError(
                f"status must be one of {sorted(allowed)}; got {value!r}"
            )
        return value


PAGES_FILENAME = "pages.json"
PANELS_FILENAME = "panels.json"


def dump_json(data: Any, *, indent: int = 2) -> str:
    return json.dumps(data, ensure_ascii=False, indent=indent, default=str)


def write_panel_records(path: str | Path, records: list[PanelRecord]) -> Path:
    """Persist a list of panel records to ``path`` as JSON."""

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = [r.model_dump(mode="json") for r in records]
    dest.write_text(dump_json(payload), encoding="utf-8")
    return dest


def load_panel_records(path: str | Path) -> list[PanelRecord]:
    """Load panel records from a JSON file written by :func:`write_panel_records`."""

    src = Path(path)
    if not src.exists():
        return []
    raw = json.loads(src.read_text("utf-8"))
    if not isinstance(raw, list):
        raise ValueError("panels.json must contain a JSON array")
    return [PanelRecord.model_validate(item) for item in raw]


__all__ = [
    "PANEL_ID_RE",
    "PAGES_FILENAME",
    "PANELS_FILENAME",
    "PanelBorder",
    "PanelLayout",
    "PanelRecord",
    "dump_json",
    "write_panel_records",
    "load_panel_records",
]
