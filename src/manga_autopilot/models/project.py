"""Pydantic models for the Manga Autopilot project root.

Spec reference: ``docs/comfyui_manga_autopilot_spec.md`` sections 7.2 (state
machine), 8 (input), and Appendix A (minimum project.json).

Only the fields needed at this stage of the project are included.  More
fields (story, characters, pages, panels, ...) live in dedicated modules and
are stitched together by the project manager service.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# State labels follow spec section 7.2.
ProjectStatus = Literal[
    "PROJECT_CREATED",
    "INPUT_VALIDATED",
    "STORY_PLANNED",
    "CHARACTERS_DEFINED",
    "CHARACTER_SHEETS_GENERATED",
    "PAGES_PLANNED",
    "PANELS_PLANNED",
    "PROMPTS_GENERATED",
    "WORKFLOWS_BUILT",
    "PANELS_GENERATING",
    "PANELS_QA_CHECKING",
    "PANELS_REPAIRING",
    "LETTERING",
    "PAGE_RENDERING",
    "EXPORTING",
    "COMPLETED",
]

OutputFormat = Literal["png_pages", "webtoon", "pdf"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectGenerationSettings(BaseModel):
    candidate_count: int = 4
    max_retry_per_panel: int = 5
    quality_threshold: float = 0.78


class ProjectSettings(BaseModel):
    page_count: int = 4
    format: list[OutputFormat] = Field(default_factory=lambda: ["png_pages"])
    generation: ProjectGenerationSettings = Field(default_factory=ProjectGenerationSettings)


class Project(BaseModel):
    """The persisted project root document (``project.json``)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    title: str | None = None
    idea: str | None = None
    language: str = "ja"
    status: ProjectStatus = "PROJECT_CREATED"
    settings: ProjectSettings = Field(default_factory=ProjectSettings)
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


__all__ = [
    "OutputFormat",
    "Project",
    "ProjectGenerationSettings",
    "ProjectSettings",
    "ProjectStatus",
]
