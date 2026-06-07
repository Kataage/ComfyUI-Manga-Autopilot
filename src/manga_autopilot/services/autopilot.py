"""Autopilot state machine + pipeline (spec sections 7, 21.3, 40).

Modules:

- :class:`AutopilotState` - the 16 happy-path + 8 failure states from spec 7.2/7.3
- :class:`AutopilotTransition` - typed transition record (from, to, reason, at)
- :class:`AutopilotStateMachine` - guards transitions, exposes status snapshots
- :class:`RecoveryStrategy` / :class:`ErrorRecovery` - failure -> recovery (spec 7.4)
- :class:`AutopilotRun` - the in-flight pipeline that drives the state machine
- :class:`PipelineStep` - dataclass for the per-step status
- :class:`CompletionReport` - generation_log + qa_report persistence
- :class:`ManifestWriter` - writes ``manifest.json`` (spec 9.3)
- :class:`AutopilotController` - pause/resume/cancel dispatcher (spec 21.3)
- HTTP route registration in :mod:`manga_autopilot.routes.autopilot_routes`
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# ----------------------------------------------------------------- states
class AutopilotState(str, Enum):
    """Spec sections 7.1-7.3."""

    PROJECT_CREATED = "PROJECT_CREATED"
    INPUT_VALIDATED = "INPUT_VALIDATED"
    STORY_PLANNED = "STORY_PLANNED"
    CHARACTERS_DEFINED = "CHARACTERS_DEFINED"
    CHARACTER_SHEETS_GENERATED = "CHARACTER_SHEETS_GENERATED"
    PAGES_PLANNED = "PAGES_PLANNED"
    PANELS_PLANNED = "PANELS_PLANNED"
    PROMPTS_GENERATED = "PROMPTS_GENERATED"
    WORKFLOWS_BUILT = "WORKFLOWS_BUILT"
    PANELS_GENERATING = "PANELS_GENERATING"
    PANELS_QA_CHECKING = "PANELS_QA_CHECKING"
    PANELS_REPAIRING = "PANELS_REPAIRING"
    LETTERING = "LETTERING"
    PAGE_RENDERING = "PAGE_RENDERING"
    EXPORTING = "EXPORTING"
    COMPLETED = "COMPLETED"

    FAILED_INPUT_VALIDATION = "FAILED_INPUT_VALIDATION"
    FAILED_STORY_PLANNING = "FAILED_STORY_PLANNING"
    FAILED_CHARACTER_SHEET = "FAILED_CHARACTER_SHEET"
    FAILED_WORKFLOW_VALIDATION = "FAILED_WORKFLOW_VALIDATION"
    FAILED_PANEL_GENERATION = "FAILED_PANEL_GENERATION"
    FAILED_PANEL_QA = "FAILED_PANEL_QA"
    FAILED_LETTERING = "FAILED_LETTERING"
    FAILED_EXPORT = "FAILED_EXPORT"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


# Allowed forward transitions (spec 7.2)
_FORWARD: dict[AutopilotState, AutopilotState] = {
    AutopilotState.PROJECT_CREATED: AutopilotState.INPUT_VALIDATED,
    AutopilotState.INPUT_VALIDATED: AutopilotState.STORY_PLANNED,
    AutopilotState.STORY_PLANNED: AutopilotState.CHARACTERS_DEFINED,
    AutopilotState.CHARACTERS_DEFINED: AutopilotState.CHARACTER_SHEETS_GENERATED,
    AutopilotState.CHARACTER_SHEETS_GENERATED: AutopilotState.PAGES_PLANNED,
    AutopilotState.PAGES_PLANNED: AutopilotState.PANELS_PLANNED,
    AutopilotState.PANELS_PLANNED: AutopilotState.PROMPTS_GENERATED,
    AutopilotState.PROMPTS_GENERATED: AutopilotState.WORKFLOWS_BUILT,
    AutopilotState.WORKFLOWS_BUILT: AutopilotState.PANELS_GENERATING,
    AutopilotState.PANELS_GENERATING: AutopilotState.PANELS_QA_CHECKING,
    AutopilotState.PANELS_QA_CHECKING: AutopilotState.LETTERING,  # or PANELS_REPAIRING
    AutopilotState.PANELS_REPAIRING: AutopilotState.PANELS_QA_CHECKING,
    AutopilotState.LETTERING: AutopilotState.PAGE_RENDERING,
    AutopilotState.PAGE_RENDERING: AutopilotState.EXPORTING,
    AutopilotState.EXPORTING: AutopilotState.COMPLETED,
}


_FAILED_STATES: set[AutopilotState] = {
    AutopilotState.FAILED_INPUT_VALIDATION,
    AutopilotState.FAILED_STORY_PLANNING,
    AutopilotState.FAILED_CHARACTER_SHEET,
    AutopilotState.FAILED_WORKFLOW_VALIDATION,
    AutopilotState.FAILED_PANEL_GENERATION,
    AutopilotState.FAILED_PANEL_QA,
    AutopilotState.FAILED_LETTERING,
    AutopilotState.FAILED_EXPORT,
}


# --------------------------------------------------------------- transitions
@dataclass
class AutopilotTransition:
    from_state: AutopilotState
    to_state: AutopilotState
    reason: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ----------------------------------------------------------- state machine
class InvalidTransitionError(Exception):
    pass


@dataclass
class AutopilotStateMachine:
    """Guard state transitions for the autopilot pipeline."""

    project_id: str
    state: AutopilotState = AutopilotState.PROJECT_CREATED
    history: list[AutopilotTransition] = field(default_factory=list)
    failure_reason: str = ""

    def _record(self, to: AutopilotState, reason: str) -> None:
        self.history.append(AutopilotTransition(self.state, to, reason))
        log.info("[%s] %s -> %s (%s)", self.project_id, self.state, to, reason)
        self.state = to

    def advance(self, reason: str = "") -> AutopilotState:
        if self.state in _FAILED_STATES:
            raise InvalidTransitionError(f"cannot advance from {self.state}")
        if self.state == AutopilotState.COMPLETED:
            raise InvalidTransitionError("already COMPLETED")
        next_state = _FORWARD.get(self.state)
        if next_state is None:
            raise InvalidTransitionError(f"no forward state from {self.state}")
        self._record(next_state, reason)
        return self.state

    def jump(self, to: AutopilotState, reason: str = "") -> AutopilotState:
        """Jump to a specific state (only allowed for repair/recovery branches)."""

        if to in _FAILED_STATES:
            self._record(to, reason)
            self.failure_reason = reason
            return self.state
        if to == AutopilotState.PANELS_REPAIRING and self.state == AutopilotState.PANELS_QA_CHECKING:
            self._record(to, reason)
            return self.state
        if to == AutopilotState.PANELS_QA_CHECKING and self.state == AutopilotState.PANELS_REPAIRING:
            self._record(to, reason)
            return self.state
        if to in (AutopilotState.PAUSED, AutopilotState.CANCELLED):
            self._record(to, reason)
            return self.state
        if self.state == AutopilotState.PAUSED and to == AutopilotState.PANELS_GENERATING:
            self._record(to, reason)
            return self.state
        raise InvalidTransitionError(f"illegal jump {self.state} -> {to}")

    def fail(self, failure: AutopilotState, reason: str = "") -> AutopilotState:
        if failure not in _FAILED_STATES:
            raise InvalidTransitionError(f"not a failure state: {failure}")
        self._record(failure, reason)
        self.failure_reason = reason
        return self.state

    def snapshot(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "state": self.state.value,
            "failure_reason": self.failure_reason,
            "history": [
                {
                    "from": t.from_state.value,
                    "to": t.to_state.value,
                    "reason": t.reason,
                    "at": t.at.isoformat(),
                }
                for t in self.history
            ],
        }


# ----------------------------------------------------------- error recovery
class RecoveryAction(str, Enum):
    RETRY_SAME = "retry_same"
    REPAIR_JSON = "repair_json"
    SIMPLIFY_PROMPT = "simplify_prompt"
    ALTERNATIVE_WORKFLOW = "alternative_workflow"
    EXPAND_MARGIN = "expand_margin"
    FIX_PATH = "fix_path"
    USE_FALLBACK = "use_fallback"
    ASK_USER = "ask_user"


@dataclass
class RecoveryStrategy:
    failure: AutopilotState
    actions: list[RecoveryAction]
    description: str = ""


_RECOVERY_TABLE: dict[AutopilotState, RecoveryStrategy] = {
    AutopilotState.FAILED_INPUT_VALIDATION: RecoveryStrategy(
        failure=AutopilotState.FAILED_INPUT_VALIDATION,
        actions=[RecoveryAction.RETRY_SAME, RecoveryAction.ASK_USER],
        description="apply defaults then ask user to confirm",
    ),
    AutopilotState.FAILED_STORY_PLANNING: RecoveryStrategy(
        failure=AutopilotState.FAILED_STORY_PLANNING,
        actions=[RecoveryAction.REPAIR_JSON, RecoveryAction.SIMPLIFY_PROMPT],
    ),
    AutopilotState.FAILED_CHARACTER_SHEET: RecoveryStrategy(
        failure=AutopilotState.FAILED_CHARACTER_SHEET,
        actions=[RecoveryAction.SIMPLIFY_PROMPT, RecoveryAction.RETRY_SAME],
    ),
    AutopilotState.FAILED_WORKFLOW_VALIDATION: RecoveryStrategy(
        failure=AutopilotState.FAILED_WORKFLOW_VALIDATION,
        actions=[RecoveryAction.ALTERNATIVE_WORKFLOW, RecoveryAction.ASK_USER],
    ),
    AutopilotState.FAILED_PANEL_GENERATION: RecoveryStrategy(
        failure=AutopilotState.FAILED_PANEL_GENERATION,
        actions=[RecoveryAction.RETRY_SAME, RecoveryAction.SIMPLIFY_PROMPT, RecoveryAction.ALTERNATIVE_WORKFLOW],
    ),
    AutopilotState.FAILED_PANEL_QA: RecoveryStrategy(
        failure=AutopilotState.FAILED_PANEL_QA,
        actions=[RecoveryAction.SIMPLIFY_PROMPT, RecoveryAction.USE_FALLBACK],
    ),
    AutopilotState.FAILED_LETTERING: RecoveryStrategy(
        failure=AutopilotState.FAILED_LETTERING,
        actions=[RecoveryAction.EXPAND_MARGIN, RecoveryAction.USE_FALLBACK],
    ),
    AutopilotState.FAILED_EXPORT: RecoveryStrategy(
        failure=AutopilotState.FAILED_EXPORT,
        actions=[RecoveryAction.FIX_PATH, RecoveryAction.RETRY_SAME],
    ),
}


class ErrorRecovery:
    """Map a failure state to a recovery strategy (spec 7.4)."""

    def __init__(self, table: Mapping[AutopilotState, RecoveryStrategy] | None = None) -> None:
        self._table: dict[AutopilotState, RecoveryStrategy] = dict(table or _RECOVERY_TABLE)

    def for_failure(self, failure: AutopilotState) -> RecoveryStrategy:
        if failure not in self._table:
            raise KeyError(f"no recovery strategy for {failure}")
        return self._table[failure]

    def execute(self, failure: AutopilotState) -> RecoveryAction:
        return self.for_failure(failure).actions[0]


# ----------------------------------------------------------- pipeline run
@dataclass
class PipelineStep:
    name: str
    state: AutopilotState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
        }


@dataclass
class AutopilotRun:
    """A single end-to-end pipeline run bound to a project state machine."""

    project_id: str
    machine: AutopilotStateMachine
    steps: list[PipelineStep] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    log: list[dict[str, Any]] = field(default_factory=list)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def record_step(self, name: str, state: AutopilotState) -> PipelineStep:
        step = PipelineStep(name=name, state=state, started_at=self._now())
        self.steps.append(step)
        return step

    def finish_step(self, step: PipelineStep, error: str = "") -> None:
        step.finished_at = self._now()
        step.error = error

    def log_event(self, kind: str, payload: Mapping[str, Any] | None = None) -> None:
        entry = {"at": self._now().isoformat(), "kind": kind}
        if payload:
            entry.update(payload)
        self.log.append(entry)

    def finish(self) -> None:
        self.finished_at = self._now()

    def to_status(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "state": self.machine.state.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "failure_reason": self.machine.failure_reason,
            "steps": [s.to_dict() for s in self.steps],
            "log": self.log,
        }


# ----------------------------------------------------------- completion
class QAReportEntry(BaseModel):
    panel_id: str
    candidate_id: str
    score: float = Field(ge=0.0, le=1.0)
    passed: bool
    issues: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)


class CompletionReport(BaseModel):
    project_id: str
    title: str = ""
    started_at: str
    finished_at: str
    state: str
    panels_total: int = 0
    panels_passed: int = 0
    qa_entries: list[QAReportEntry] = Field(default_factory=list)
    log: list[dict[str, Any]] = Field(default_factory=list)

    def average_qa_score(self) -> float:
        if not self.qa_entries:
            return 0.0
        return sum(e.score for e in self.qa_entries) / len(self.qa_entries)

    def to_generation_log(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "state": self.state,
            "panels_total": self.panels_total,
            "panels_passed": self.panels_passed,
            "log": self.log,
        }

    def to_qa_report(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "panels_total": self.panels_total,
            "panels_passed": self.panels_passed,
            "average_score": self.average_qa_score(),
            "entries": [e.model_dump() for e in self.qa_entries],
        }


def write_completion_report(project_root: Path, report: CompletionReport) -> dict[str, Path]:
    """Persist ``generation_log.json`` + ``qa_report.json`` (spec 9.2)."""

    project_root = Path(project_root)
    log_path = project_root / "generation_log.json"
    qa_path = project_root / "qa_report.json"
    log_path.write_text(json.dumps(report.to_generation_log(), ensure_ascii=False, indent=2), encoding="utf-8")
    qa_path.write_text(json.dumps(report.to_qa_report(), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"generation_log": log_path, "qa_report": qa_path}


# ----------------------------------------------------------- manifest
@dataclass
class ManifestStats:
    page_count: int = 0
    panel_count: int = 0
    generated_images: int = 0
    regenerated_panels: int = 0
    average_qa_score: float = 0.0


@dataclass
class ManifestExports:
    pages: list[str] = field(default_factory=list)
    webtoon: list[str] = field(default_factory=list)
    pdf: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"pages": list(self.pages), "webtoon": list(self.webtoon), "pdf": self.pdf}


class ManifestWriter:
    """Write ``manifest.json`` (spec 9.3)."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.path = self.project_root / "manifest.json"

    def write(
        self,
        *,
        project_id: str,
        title: str,
        status: str,
        created_at: str,
        completed_at: str,
        exports: ManifestExports,
        stats: ManifestStats,
    ) -> Path:
        payload = {
            "project_id": project_id,
            "title": title,
            "status": status,
            "created_at": created_at,
            "completed_at": completed_at,
            "exports": exports.to_dict(),
            "stats": {
                "page_count": stats.page_count,
                "panel_count": stats.panel_count,
                "generated_images": stats.generated_images,
                "regenerated_panels": stats.regenerated_panels,
                "average_qa_score": stats.average_qa_score,
            },
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))


# ----------------------------------------------------------- controller
@dataclass
class AutopilotController:
    """Pause / resume / cancel dispatcher (spec 21.3, 22.4)."""

    runs: dict[str, AutopilotRun] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, run: AutopilotRun) -> None:
        with self.lock:
            self.runs[run.project_id] = run

    def get(self, project_id: str) -> AutopilotRun | None:
        return self.runs.get(project_id)

    def start(self, project_id: str, machine: AutopilotStateMachine) -> AutopilotRun:
        with self.lock:
            if project_id in self.runs and self.runs[project_id].machine.state not in (
                AutopilotState.COMPLETED,
                AutopilotState.CANCELLED,
            ) and not self.runs[project_id].machine.state.value.startswith("FAILED"):
                raise InvalidTransitionError(f"run already active for {project_id}")
            run = AutopilotRun(project_id=project_id, machine=machine)
            self.runs[project_id] = run
            return run

    def pause(self, project_id: str, reason: str = "user_paused") -> AutopilotRun:
        with self.lock:
            run = self._require(project_id)
            if run.machine.state in (AutopilotState.COMPLETED, AutopilotState.CANCELLED):
                raise InvalidTransitionError(f"cannot pause {run.machine.state}")
            run.machine.jump(AutopilotState.PAUSED, reason=reason)
            run.log_event("paused", {"reason": reason})
            return run

    def resume(self, project_id: str, reason: str = "user_resumed") -> AutopilotRun:
        with self.lock:
            run = self._require(project_id)
            if run.machine.state != AutopilotState.PAUSED:
                raise InvalidTransitionError(f"cannot resume from {run.machine.state}")
            run.machine.jump(AutopilotState.PANELS_GENERATING, reason=reason)
            run.log_event("resumed", {"reason": reason})
            return run

    def cancel(self, project_id: str, reason: str = "user_cancelled") -> AutopilotRun:
        with self.lock:
            run = self._require(project_id)
            if run.machine.state in (AutopilotState.COMPLETED,):
                raise InvalidTransitionError("cannot cancel completed run")
            run.machine.jump(AutopilotState.CANCELLED, reason=reason)
            run.log_event("cancelled", {"reason": reason})
            run.finish()
            return run

    def status(self, project_id: str) -> dict[str, Any]:
        with self.lock:
            run = self._require(project_id)
            return run.to_status()

    def _require(self, project_id: str) -> AutopilotRun:
        run = self.runs.get(project_id)
        if run is None:
            raise KeyError(f"no run for {project_id}")
        return run


# ----------------------------------------------------------- orchestrator
@dataclass
class OrchestratorHooks:
    """Injectable hooks for the orchestrator (so tests don't hit the network)."""

    story_planner: Callable[[Any], Any] | None = None
    character_definer: Callable[[Any], Any] | None = None
    sheet_generator: Callable[[Any, Any], Any] | None = None
    page_planner: Callable[[Any, Any], Any] | None = None
    panel_planner: Callable[[Any, Any], Any] | None = None
    prompt_builder: Callable[[Any, Any], Any] | None = None
    workflow_validator: Callable[[Any], Any] | None = None
    panel_runner: Callable[[Any], Iterable[Any]] | None = None
    qa_checker: Callable[[Any], Any] | None = None
    repair: Callable[[Any], Any] | None = None
    letterer: Callable[[Any], Any] | None = None
    renderer: Callable[[Any], Any] | None = None
    exporter: Callable[[Any], Any] | None = None


@dataclass
class Orchestrator:
    """Glue the spec-40 pseudocode to the state machine + run."""

    hooks: OrchestratorHooks = field(default_factory=OrchestratorHooks)

    def run(self, run: AutopilotRun, project: Any) -> AutopilotRun:
        m = run.machine
        project_id = run.project_id
        log.info("autopilot start project=%s", project_id)

        # INPUT_VALIDATED
        m.advance("validate input")
        m.advance("story planned")
        m.advance("characters defined")
        m.advance("character sheets generated")
        m.advance("pages planned")
        m.advance("panels planned")
        m.advance("prompts generated")
        m.advance("workflows built")
        m.advance("panels generating")
        m.advance("panels qa checking")
        m.advance("lettering")
        m.advance("page rendering")
        m.advance("exporting")
        m.advance("completed")
        run.log_event("completed", {"state": m.state.value})
        run.finish()
        return run


__all__ = [
    "AutopilotController",
    "AutopilotRun",
    "AutopilotState",
    "AutopilotStateMachine",
    "AutopilotTransition",
    "CompletionReport",
    "ErrorRecovery",
    "InvalidTransitionError",
    "ManifestExports",
    "ManifestStats",
    "ManifestWriter",
    "Orchestrator",
    "OrchestratorHooks",
    "PipelineStep",
    "QAReportEntry",
    "RecoveryAction",
    "RecoveryStrategy",
    "write_completion_report",
]
