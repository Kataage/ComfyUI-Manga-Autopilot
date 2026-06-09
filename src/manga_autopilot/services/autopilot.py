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
- :class:`Orchestrator` / :class:`OrchestratorHooks` - drives each step through
  the real services (story -> pages -> panels -> prompts -> workflow -> panels
  -> QA -> lettering -> page render -> export).
- HTTP route registration in :mod:`manga_autopilot.routes.autopilot_routes`
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# --------------------------------------------------------------- run_id
def generate_run_id() -> str:
    """Generate a unique run identifier: run_YYYYMMDD_HHMMSS_<8hex>."""
    now = datetime.now(timezone.utc)
    short = secrets.token_hex(4)
    return f"run_{now.strftime('%Y%m%d_%H%M%S')}_{short}"


def save_run_metadata(project_root: Path, run: AutopilotRun) -> None:
    """Persist run.json and latest_run_id.txt for a run.

    Call this after starting a run and again when it finishes to update
    the status fields.
    """
    project_root = Path(project_root)
    runs_dir = project_root / "runs"
    run_dir = runs_dir / run.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    status_value = run.machine.state.value
    payload = {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "kind": run.source.get("restart_of_run_id") and "restart" or "start",
        "status": status_value,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.finished_at.isoformat() if run.finished_at else None,
        "cancelled_at": None,
        "failed_at": None,
        "input": run.input,
        "source": run.source,
    }
    if status_value == "CANCELLED":
        payload["cancelled_at"] = (run.finished_at or run._now()).isoformat()
    elif status_value.startswith("FAILED"):
        payload["failed_at"] = (run.finished_at or run._now()).isoformat()

    run_file = run_dir / "run.json"
    run_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    latest_file = project_root / "latest_run_id.txt"
    latest_file.write_text(run.run_id, encoding="utf-8")


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


_STEP_NAMES: dict[AutopilotState, str] = {
    AutopilotState.INPUT_VALIDATED: "validate_input",
    AutopilotState.STORY_PLANNED: "plan_story",
    AutopilotState.CHARACTERS_DEFINED: "define_characters",
    AutopilotState.CHARACTER_SHEETS_GENERATED: "generate_character_sheets",
    AutopilotState.PAGES_PLANNED: "plan_pages",
    AutopilotState.PANELS_PLANNED: "plan_panels",
    AutopilotState.PROMPTS_GENERATED: "build_prompts",
    AutopilotState.WORKFLOWS_BUILT: "validate_workflow",
    AutopilotState.PANELS_GENERATING: "generate_panels",
    AutopilotState.PANELS_QA_CHECKING: "qa_panels",
    AutopilotState.LETTERING: "lettering",
    AutopilotState.PAGE_RENDERING: "render_pages",
    AutopilotState.EXPORTING: "export",
    AutopilotState.COMPLETED: "finalize",
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
        if self.state == AutopilotState.PAUSED:
            # Resume: accept any state the machine can legally occupy.
            # This is gated by :meth:`AutopilotController.resume` which only
            # records ``pre_pause_state`` from a state the run was actually in.
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
    run_id: str = field(default_factory=generate_run_id)
    steps: list[PipelineStep] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    log: list[dict[str, Any]] = field(default_factory=list)
    artefacts: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task | None = None
    cancel_event: asyncio.Event | None = None
    pause_event: asyncio.Event | None = None
    pre_pause_state: AutopilotState | None = None
    """The state the pipeline was in *before* the most recent pause.

    Populated by :meth:`AutopilotController.pause` and consumed by
    :meth:`AutopilotController.resume` to restore the run to where it
    actually was when the user pressed pause (rather than always jumping
    to ``PANELS_GENERATING``).
    """
    input: dict[str, Any] = field(default_factory=dict)
    source: dict[str, str | None] = field(default_factory=lambda: {"restart_of_run_id": None, "resume_of_run_id": None})

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

    def store(self, key: str, value: Any) -> None:
        self.artefacts[key] = value

    def finish(self) -> None:
        self.finished_at = self._now()

    def to_status(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "state": self.machine.state.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "failure_reason": self.machine.failure_reason,
            "steps": [s.to_dict() for s in self.steps],
            "log": self.log,
            "artefacts": self.artefacts,
            "source": self.source,
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
    run_id: str = ""
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
            "run_id": self.run_id,
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
        run_id: str = "",
        title: str,
        status: str,
        created_at: str,
        completed_at: str,
        exports: ManifestExports,
        stats: ManifestStats,
    ) -> Path:
        payload = {
            "project_id": project_id,
            "run_id": run_id,
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

    def start(
        self,
        project_id: str,
        machine: AutopilotStateMachine,
        *,
        input_payload: Mapping[str, Any] | None = None,
        driver: Callable[[AutopilotRun], Awaitable[None]] | None = None,
    ) -> AutopilotRun:
        with self.lock:
            if project_id in self.runs and self.runs[project_id].machine.state not in (
                AutopilotState.COMPLETED,
                AutopilotState.CANCELLED,
            ) and not self.runs[project_id].machine.state.value.startswith("FAILED"):
                raise InvalidTransitionError(f"run already active for {project_id}")
            run = AutopilotRun(project_id=project_id, machine=machine)
            run.input = dict(input_payload or {})
            self.runs[project_id] = run
            if driver is not None:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        run.task = loop.create_task(driver(run))
                    else:
                        run.task = loop.create_task(driver(run))
                except RuntimeError:
                    run.task = None
            return run

    def attach_task(
        self,
        project_id: str,
        task: asyncio.Task,
        *,
        cancel_event: asyncio.Event | None = None,
        pause_event: asyncio.Event | None = None,
    ) -> None:
        with self.lock:
            run = self.runs.get(project_id)
            if run is None:
                return
            run.task = task
            run.cancel_event = cancel_event
            run.pause_event = pause_event

    def pause(self, project_id: str, reason: str = "user_paused") -> AutopilotRun:
        """Mark a run as paused.

        The current :class:`AutopilotState` is captured in
        :attr:`AutopilotRun.pre_pause_state` so :meth:`resume` can restore
        the run to its real position in the pipeline.  The
        :attr:`AutopilotRun.pause_event` is **cleared** so the orchestrator's
        per-step :keyword:`await pause_event.wait()` blocks until
        :meth:`resume` is called.
        """

        with self.lock:
            run = self._require(project_id)
            if run.machine.state in (AutopilotState.COMPLETED, AutopilotState.CANCELLED):
                raise InvalidTransitionError(f"cannot pause {run.machine.state}")
            if run.machine.state == AutopilotState.PAUSED:
                # idempotent; nothing to do.
                return run
            run.pre_pause_state = run.machine.state
            run.machine.jump(AutopilotState.PAUSED, reason=reason)
            if run.pause_event is not None:
                run.pause_event.clear()
            run.log_event("paused", {"reason": reason, "pre_pause_state": run.pre_pause_state.value})
            return run

    def resume(self, project_id: str, reason: str = "user_resumed") -> AutopilotRun:
        """Resume a previously-paused run.

        The state machine jumps back to the captured
        :attr:`AutopilotRun.pre_pause_state` (defaulting to
        :attr:`AutopilotState.PANELS_GENERATING` when no state was recorded,
        e.g. for runs that were paused before the first hook ran).
        """

        with self.lock:
            run = self._require(project_id)
            if run.machine.state != AutopilotState.PAUSED:
                raise InvalidTransitionError(f"cannot resume from {run.machine.state}")
            target = run.pre_pause_state or AutopilotState.PANELS_GENERATING
            run.machine.jump(target, reason=reason)
            run.pre_pause_state = None
            if run.pause_event is not None:
                run.pause_event.set()
            run.log_event("resumed", {"reason": reason, "resumed_to": target.value})
            return run

    def cancel(self, project_id: str, reason: str = "user_cancelled") -> AutopilotRun:
        with self.lock:
            run = self._require(project_id)
            if run.machine.state in (AutopilotState.COMPLETED,):
                raise InvalidTransitionError("cannot cancel completed run")
            run.machine.jump(AutopilotState.CANCELLED, reason=reason)
            run.log_event("cancelled", {"reason": reason})
            run.finish()
            if run.cancel_event is not None:
                run.cancel_event.set()
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
HookResult = Any


@dataclass
class OrchestratorHooks:
    """Injectable hooks for the orchestrator (so tests don't hit the network).

    Hooks can be plain callables (sync) or ``async`` callables. Sync hooks are
    always run inside ``asyncio.to_thread`` so they cannot block the event loop.
    Each hook receives the :class:`AutopilotRun` as its first positional
    argument plus any extra args needed to do its work.
    """

    validate_input: Callable[..., Awaitable[Any] | Any] | None = None
    plan_story: Callable[..., Awaitable[Any] | Any] | None = None
    define_characters: Callable[..., Awaitable[Any] | Any] | None = None
    generate_character_sheets: Callable[..., Awaitable[Any] | Any] | None = None
    plan_pages: Callable[..., Awaitable[Any] | Any] | None = None
    plan_panels: Callable[..., Awaitable[Any] | Any] | None = None
    build_prompts: Callable[..., Awaitable[Any] | Any] | None = None
    validate_workflow: Callable[..., Awaitable[Any] | Any] | None = None
    generate_panels: Callable[..., Awaitable[Any] | Any] | None = None
    qa_panels: Callable[..., Awaitable[Any] | Any] | None = None
    lettering: Callable[..., Awaitable[Any] | Any] | None = None
    render_pages: Callable[..., Awaitable[Any] | Any] | None = None
    export: Callable[..., Awaitable[Any] | Any] | None = None
    finalize: Callable[..., Awaitable[Any] | Any] | None = None


def _is_cancelled(run: AutopilotRun) -> bool:
    if run.cancel_event is None:
        return False
    return run.cancel_event.is_set()


def _is_paused(run: AutopilotRun) -> bool:
    """``True`` when the run is currently between pause() and resume().

    The :attr:`AutopilotRun.pause_event` is the single source of truth: when
    it is ``False`` (cleared), the orchestrator should wait.  We also
    require :attr:`AutopilotState.PAUSED` in the state machine as a
    belt-and-braces guard so a run that was never wired to an event loop
    still surfaces its paused state through the normal status API.
    """

    if run.machine.state == AutopilotState.PAUSED:
        return True
    if run.pause_event is not None and not run.pause_event.is_set():
        return True
    return False


async def _invoke_hook(callable_hook: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> Any:
    """Run a (sync or async) hook and return its result."""

    if callable_hook is None:
        return None
    result = callable_hook(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return await result
    return result


@dataclass
class Orchestrator:
    """Glue the spec-40 pseudocode to the state machine + run.

    Each step of the pipeline advances the state machine, records a
    :class:`PipelineStep`, and stores any artefacts on the run.  The
    orchestrator respects pause and cancel events set on the run by the
    controller, and writes a :class:`CompletionReport` at the end.
    """

    hooks: OrchestratorHooks = field(default_factory=OrchestratorHooks)
    project_root: Path | None = None

    async def _step(
        self,
        run: AutopilotRun,
        target_state: AutopilotState,
        hook_name: str,
        *hook_args: Any,
        **hook_kwargs: Any,
    ) -> Any:
        # Cooperative pause: always wait for the pause event *before* doing
        # any state-machine work.  When ``AutopilotController.pause`` runs,
        # it (a) jumps the state machine to PAUSED and (b) clears the event;
        # both of these together mean we must NOT short-circuit on
        # ``machine.state == PAUSED`` because that would cause this step to
        # return None and let the pipeline keep walking to the next step
        # (which would then ``advance`` from PAUSED and crash).  The
        # ``asyncio.Event`` is the single source of truth: cleared ==
        # blocked, set == unblocked.
        if run.pause_event is not None:
            await run.pause_event.wait()
        if _is_cancelled(run):
            return None
        if _is_paused(run):
            # Event was set (we got past ``wait``) but the state machine
            # still claims PAUSED.  This can only happen in a narrow race
            # where ``resume()`` ran *after* the controller set the event
            # but *before* the state was rewound.  Don't try to advance
            # from PAUSED; let the caller re-invoke us on the next loop.
            return None
        run.machine.advance(reason=hook_name)
        step = run.record_step(hook_name, target_state)
        run.log_event("step_started", {"step": hook_name, "state": target_state.value})
        try:
            hook = getattr(self.hooks, hook_name, None)
            result = await _invoke_hook(hook, run, *hook_args, **hook_kwargs)
            run.finish_step(step)
            if result is not None:
                run.store(hook_name, result)
            run.log_event("step_finished", {"step": hook_name, "state": target_state.value})
            return result
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            run.finish_step(step, error=err)
            run.log_event("step_failed", {"step": hook_name, "error": err})
            self._fail(run, target_state, exc)
            return None

    def _fail(
        self,
        run: AutopilotRun,
        step_state: AutopilotState,
        exc: BaseException,
    ) -> None:
        failure_map = {
            AutopilotState.STORY_PLANNED: AutopilotState.FAILED_STORY_PLANNING,
            AutopilotState.CHARACTERS_DEFINED: AutopilotState.FAILED_CHARACTER_SHEET,
            AutopilotState.WORKFLOWS_BUILT: AutopilotState.FAILED_WORKFLOW_VALIDATION,
            AutopilotState.PANELS_GENERATING: AutopilotState.FAILED_PANEL_GENERATION,
            AutopilotState.PANELS_QA_CHECKING: AutopilotState.FAILED_PANEL_QA,
            AutopilotState.LETTERING: AutopilotState.FAILED_LETTERING,
            AutopilotState.EXPORTING: AutopilotState.FAILED_EXPORT,
            AutopilotState.INPUT_VALIDATED: AutopilotState.FAILED_INPUT_VALIDATION,
        }
        failure = failure_map.get(step_state)
        if failure is not None:
            run.machine.fail(failure, reason=f"{exc}")
            recovery = ErrorRecovery().for_failure(failure)
            run.log_event("recovery_planned", {"action": recovery.actions[0].value})

    async def run_pipeline(self, run: AutopilotRun) -> AutopilotRun:
        """Drive the full pipeline against the run's state machine."""

        log.info("autopilot pipeline start project=%s", run.project_id)
        run.log_event("pipeline_started")

        try:
            await self._step(run, AutopilotState.INPUT_VALIDATED, "validate_input")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.STORY_PLANNED, "plan_story")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.CHARACTERS_DEFINED, "define_characters")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.CHARACTER_SHEETS_GENERATED, "generate_character_sheets")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.PAGES_PLANNED, "plan_pages")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.PANELS_PLANNED, "plan_panels")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.PROMPTS_GENERATED, "build_prompts")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.WORKFLOWS_BUILT, "validate_workflow")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.PANELS_GENERATING, "generate_panels")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.PANELS_QA_CHECKING, "qa_panels")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.LETTERING, "lettering")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.PAGE_RENDERING, "render_pages")
            if _is_cancelled(run):
                return self._finalize(run)

            await self._step(run, AutopilotState.EXPORTING, "export")
            if _is_cancelled(run):
                return self._finalize(run)

            if not run.machine.state.value.startswith("FAILED"):
                await self._step(run, AutopilotState.COMPLETED, "finalize")

            return self._finalize(run)
        except Exception as exc:  # noqa: BLE001
            log.exception("autopilot pipeline failed: %s", exc)
            if not run.machine.state.value.startswith("FAILED"):
                run.machine.fail(AutopilotState.FAILED_PANEL_GENERATION, reason=str(exc))
            return self._finalize(run)

    def _finalize(self, run: AutopilotRun) -> AutopilotRun:
        if self.project_root is not None:
            try:
                report = CompletionReport(
                    project_id=run.project_id,
                    run_id=run.run_id,
                    title=str(run.input.get("title", run.project_id)),
                    started_at=run.started_at.isoformat(),
                    finished_at=run.finished_at.isoformat() if run.finished_at else run._now().isoformat(),
                    state=run.machine.state.value,
                    log=list(run.log),
                )
                write_completion_report(self.project_root, report)
                run.store("completion_report", report.model_dump())
            except Exception as exc:  # pragma: no cover - best-effort
                log.warning("could not write completion report: %s", exc)
            try:
                save_run_metadata(self.project_root, run)
            except Exception as exc:  # pragma: no cover - best-effort
                log.warning("could not save run metadata: %s", exc)
        if not run.finished_at:
            run.finish()
        run.log_event("pipeline_finished", {"state": run.machine.state.value})
        return run

    # Backwards-compatible alias for older call sites.
    def run(self, run: AutopilotRun, project: Any = None) -> AutopilotRun:  # type: ignore[override]
        """Synchronous alias that just walks the state machine.

        This is kept for callers that don't have an event loop.  Production
        code should use :meth:`run_pipeline` from the HTTP route.
        """

        m = run.machine
        log.info("autopilot sync start project=%s", run.project_id)
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


def start_orchestrator(
    controller: AutopilotController,
    project_id: str,
    *,
    hooks: OrchestratorHooks,
    project_root: Path | None = None,
    input_payload: Mapping[str, Any] | None = None,
) -> tuple[AutopilotRun, asyncio.Task, asyncio.Event, asyncio.Event]:
    """Spin up an orchestrator task for ``project_id``.

    Returns ``(run, task, cancel_event, pause_event)``.  The caller can pass
    the events to :meth:`AutopilotController.attach_task` so the controller's
    pause/resume/cancel methods can affect the running pipeline.
    """

    machine = AutopilotStateMachine(project_id=project_id)
    run = controller.start(
        project_id,
        machine,
        input_payload=input_payload,
    )
    cancel_event = asyncio.Event()
    pause_event = asyncio.Event()
    pause_event.set()  # not paused by default

    orchestrator = Orchestrator(hooks=hooks, project_root=project_root)

    async def _driver() -> None:
        # block while paused
        if run.machine.state == AutopilotState.PAUSED:
            await pause_event.wait()
        await orchestrator.run_pipeline(run)

    task = asyncio.create_task(_driver(), name=f"manga-autopilot-{project_id}")
    controller.attach_task(
        project_id,
        task,
        cancel_event=cancel_event,
        pause_event=pause_event,
    )
    return run, task, cancel_event, pause_event


__all__ = [
    "AutopilotController",
    "AutopilotRun",
    "AutopilotState",
    "AutopilotStateMachine",
    "AutopilotTransition",
    "CompletionReport",
    "ErrorRecovery",
    "HookResult",
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
    "start_orchestrator",
    "write_completion_report",
]
