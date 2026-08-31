"""GenerationJob persistence model (spec section 17.1, 17.5).

This module exposes the on-disk representation of a single panel's
generation run.  A :class:`GenerationJob` records the input, the candidate
images that were considered, the selected candidate, retry counts and
final status -- everything an operator needs to audit or replay the
generation.

Only metadata is persisted to disk; the in-memory ``PIL.Image.Image``
candidates live in :class:`manga_autopilot.services.qa.CandidateImage`
and are not embedded in the JSON document.  Instead, each candidate
carries an ``image_path`` pointing to its saved PNG under
``{storage_root}/projects/{project_id}/assets/panels/``.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class JobStatus(str, Enum):
    """Lifecycle states for a :class:`GenerationJob` (spec section 17.2).

    Happy path: PENDING -> VALIDATING -> QUEUED -> RUNNING ->
    FETCHING_RESULT -> QA_CHECKING -> COMPLETED.

    On retry: ... -> QA_CHECKING -> RETRYING -> QUEUED -> ...
    On terminal failure: any state -> FAILED / CANCELLED.

    Strict Anima runs add AWAITING_REVIEW: a quality rejection stops there
    and waits for the user instead of retrying automatically.
    """

    PENDING = "pending"
    VALIDATING = "validating"
    QUEUED = "queued"
    RUNNING = "running"
    FETCHING_RESULT = "fetching_result"
    QA_CHECKING = "qa_checking"
    RETRYING = "retrying"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


class CandidateImageMeta(BaseModel):
    """A persisted candidate image record.

    Mirrors :class:`manga_autopilot.services.qa.CandidateImage` minus the
    in-memory ``Image.Image`` payload, which lives on disk under
    ``image_path``.
    """

    model_config = {"extra": "ignore"}

    candidate_id: str = Field(min_length=1, max_length=128)
    panel_id: str = Field(min_length=1, max_length=128)
    seed: int
    workflow_id: str = ""
    image_path: str | None = None
    width: int = 0
    height: int = 0
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    passed: bool | None = None
    selected: bool = False
    qa_actions: list[str] = Field(default_factory=list)


class GenerationJob(BaseModel):
    """A single panel's generation run, persisted as JSON.

    Stored at ``{project_root}/jobs/{job_id}.json``.  Only metadata is
    persisted -- the actual generated image bytes live in
    ``assets/panels/`` and are referenced from :attr:`CandidateImageMeta.image_path`.
    """

    model_config = {"extra": "ignore"}

    id: str = Field(default_factory=_new_job_id)
    project_id: str = Field(min_length=1, max_length=128)
    page_number: int = Field(ge=1, le=9999)
    panel_id: str = Field(min_length=1, max_length=128)
    workflow_id: str = "anime_t2i_default"
    prompt_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    candidates: list[CandidateImageMeta] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    status: JobStatus = JobStatus.PENDING
    error: str = ""
    fallback_used: bool = False
    created_at: str = Field(default_factory=_utc_now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str = Field(default_factory=_utc_now_iso)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not JOB_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"job id {value!r} must match {JOB_ID_PATTERN.pattern}"
            )
        return value

    # ------------------------------------------------------------------ utils
    def touch(self) -> None:
        self.updated_at = _utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GenerationJob:
        return cls.model_validate(dict(data))

    def select_candidate(self, candidate_id: str) -> None:
        for cand in self.candidates:
            cand.selected = cand.candidate_id == candidate_id
        self.selected_candidate_id = candidate_id

    def selected_candidate(self) -> CandidateImageMeta | None:
        if not self.selected_candidate_id:
            return None
        for cand in self.candidates:
            if cand.candidate_id == self.selected_candidate_id:
                return cand
        return None


def write_job(path: Path, job: GenerationJob) -> Path:
    """Persist ``job`` to ``path`` as UTF-8 JSON."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    job.touch()
    path.write_text(
        json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def read_job(path: Path) -> GenerationJob:
    """Load a :class:`GenerationJob` from ``path`` (must exist)."""

    return GenerationJob.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


__all__ = [
    "CandidateImageMeta",
    "GenerationJob",
    "JOB_ID_PATTERN",
    "JobStatus",
    "read_job",
    "write_job",
]
