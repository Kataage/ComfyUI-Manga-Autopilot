"""Opt-in v2 Work-DB adapter for the existing Autopilot pipeline (#246).

Legacy project routes keep their existing behavior. This adapter runs the
same OrchestratorHooks against Work DB-authoritative Runs and RunSteps.
Inputs and hook outputs must be JSON-serializable; failing to persist the
result is a failed step, never an untracked completion.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from manga_autopilot.primitives import canonical_json, input_fingerprint
from manga_autopilot.repositories.durable_runs import (
    DurableRunRepository,
    DurableRunStateError,
    WorkLeaseConflictError,
)
from manga_autopilot.services.autopilot import (
    _STEP_NAMES,
    AutopilotRun,
    AutopilotState,
    AutopilotStateMachine,
    Orchestrator,
    OrchestratorHooks,
    _invoke_hook,
)


class RetryableStepError(RuntimeError):
    """The hook is explicitly safe to retry after inspecting its outcome."""


class NeedsAttentionStepError(RuntimeError):
    """The hook requires human reconciliation before being resumed."""


class InterruptedStepError(RuntimeError):
    """The hook reports an interrupted or uncertain operation."""


def _json_value(value: Any) -> Any:
    """Use only data that survives a new process and exact JSON replay."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.loads(canonical_json(value))


@dataclass
class DurableAutopilotOrchestrator:
    """Opt-in bridge; Work DB, not in-memory state, decides which hooks run.

    Caller supplies the persisted Run id and the relevant stage inputs.
    Dependencies form an ordered fingerprint chain, so changing one stage
    invalidates all subsequent stages. An interrupted in-flight hook is never
    replayed without explicit caller reconciliation.
    """

    repository: DurableRunRepository
    work_id: str
    hooks: OrchestratorHooks = field(default_factory=OrchestratorHooks)
    project_root: Path | None = None
    lease_ttl_seconds: int = 3600

    async def execute(
        self,
        run_id: str,
        *,
        input_payload: Mapping[str, Any],
        step_inputs: Mapping[str, Any],
        lease_owner: str | None = None,
        reclaim_expired_owner: str | None = None,
        approve_interrupted_retry: bool = False,
        approve_needs_attention_retry: bool = False,
    ) -> AutopilotRun:
        """Execute/resume a Work-scoped Run with persist-first step outcomes."""
        if self.lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        if not isinstance(input_payload, Mapping) or not isinstance(step_inputs, Mapping):
            raise TypeError("durable inputs must be mappings")
        durable = self.repository.get_run(run_id)
        if durable["scope_type"] != "WORK" or durable["scope_id"] != self.work_id:
            raise DurableRunStateError("Run is not owned by this Work")
        if durable["status"] in {"COMPLETED", "FAILED_TERMINAL", "CANCELLED"}:
            raise DurableRunStateError("terminal Run cannot resume; create a new Run")
        if durable["status"] == "NEEDS_ATTENTION" and not approve_needs_attention_retry:
            raise DurableRunStateError("Run requires explicit human reconciliation")
        if durable["status"] == "INTERRUPTED" and not approve_interrupted_retry:
            raise DurableRunStateError("interrupted Run requires explicit reconciliation")

        base_inputs = _json_value(dict(input_payload))
        stage_inputs = _json_value(dict(step_inputs))
        owner = lease_owner or f"worker_{secrets.token_hex(16)}"
        machine = AutopilotStateMachine(project_id=self.work_id)
        run = AutopilotRun(
            project_id=self.work_id, machine=machine, run_id=run_id,
        )
        run.input = base_inputs
        legacy = Orchestrator(hooks=self.hooks, project_root=self.project_root)

        # Obtaining the single Work lease precedes any durable Run mutation.
        # An expired owner is never reclaimed automatically.
        self.repository.acquire_lease(
            work_id=self.work_id,
            lease_owner=owner,
            lease_kind="AUTOPILOT_MUTATION",
            ttl_seconds=self.lease_ttl_seconds,
            run_id=run_id,
            reclaim_expired_owner=reclaim_expired_owner,
        )
        try:
            durable = self.repository.get_run(run_id)
            if durable["status"] == "RUNNING":
                self.repository.recover_interrupted_run(
                    run_id, lease_owner=owner,
                )
                durable = self.repository.get_run(run_id)
                if not approve_interrupted_retry and any(
                    step["status"] == "INTERRUPTED"
                    for step in self.repository.list_steps(run_id)
                ):
                    raise DurableRunStateError(
                        "crashed in-flight step needs explicit reconciliation"
                    )
            self.repository.transition_run(
                run_id, expected_status=str(durable["status"]),
                new_status="RUNNING", lease_owner=owner,
            )
            upstream = input_fingerprint({
                "run_kind": durable["run_kind"],
                "run_input": base_inputs,
            })
            current_steps = {
                step["step_key"]: step
                for step in self.repository.list_steps(run_id)
            }
            for target_state, hook_name in _STEP_NAMES.items():
                fingerprint = input_fingerprint({
                    "step_key": hook_name,
                    "stage_input": stage_inputs.get(hook_name),
                    "upstream": upstream,
                })
                upstream = fingerprint
                step = current_steps.get(hook_name)
                if step is None:
                    step = self.repository.create_step(
                        run_id=run_id, step_key=hook_name,
                        input_fingerprint=fingerprint,
                        lease_owner=owner,
                    )
                    current_steps[hook_name] = step

                if step["status"] == "COMPLETED":
                    if step["input_fingerprint"] == fingerprint:
                        stored = json.loads(step["output_json"])
                        if "value" not in stored:
                            raise DurableRunStateError(
                                "completed RunStep lacks replayable output"
                            )
                        machine.advance(reason=f"persisted:{hook_name}")
                        value = stored["value"]
                        if value is not None:
                            run.store(hook_name, value)
                        run.log_event("step_skipped", {"step": hook_name})
                        continue
                    self.repository.mark_step_stale(
                        step["id"], new_fingerprint=fingerprint,
                        lease_owner=owner,
                    )
                elif step["status"] == "PENDING" and (
                    step["input_fingerprint"] != fingerprint
                ):
                    self.repository.set_pending_fingerprint(
                        step["id"], input_fingerprint=fingerprint,
                        lease_owner=owner,
                    )
                elif step["status"] == "INTERRUPTED" and (
                    not approve_interrupted_retry
                ):
                    raise DurableRunStateError(
                        f"interrupted step {hook_name} needs reconciliation"
                    )
                elif step["status"] == "NEEDS_ATTENTION" and (
                    not approve_needs_attention_retry
                ):
                    raise DurableRunStateError(
                        f"step {hook_name} needs human reconciliation"
                    )
                elif step["status"] == "FAILED_TERMINAL":
                    raise DurableRunStateError(
                        f"terminal failed step {hook_name} cannot retry"
                    )

                # No untracked result is treated as successful completion.
                self.repository.start_step(
                    step["id"], input_fingerprint=fingerprint,
                    lease_owner=owner,
                )
                machine.advance(reason=hook_name)
                memory_step = run.record_step(hook_name, target_state)
                run.log_event("step_started", {"step": hook_name})
                try:
                    result = await _invoke_hook(
                        getattr(self.hooks, hook_name, None), run,
                    )
                    replayable = _json_value(result)
                    self.repository.finish_step(
                        step["id"], status="COMPLETED",
                        output={"value": replayable}, lease_owner=owner,
                    )
                    run.finish_step(memory_step)
                    if replayable is not None:
                        run.store(hook_name, replayable)
                    run.log_event("step_finished", {"step": hook_name})
                except asyncio.CancelledError:
                    self.repository.finish_step(
                        step["id"], status="INTERRUPTED",
                        error={"reason": "task_cancelled"},
                        lease_owner=owner,
                    )
                    self.repository.transition_run(
                        run_id, expected_status="RUNNING",
                        new_status="INTERRUPTED", lease_owner=owner,
                    )
                    raise
                except Exception as exc:  # noqa: BLE001
                    if isinstance(exc, RetryableStepError):
                        state = "FAILED_RETRYABLE"
                    elif isinstance(exc, NeedsAttentionStepError):
                        state = "NEEDS_ATTENTION"
                    elif isinstance(exc, InterruptedStepError):
                        state = "INTERRUPTED"
                    else:
                        # Unknown/possibly side-effectful failures are not
                        # silently retried with a fresh stochastic attempt.
                        state = "FAILED_TERMINAL"
                    error = {"type": type(exc).__name__, "message": str(exc)}
                    self.repository.finish_step(
                        step["id"], status=state, error=error,
                        lease_owner=owner,
                    )
                    self.repository.transition_run(
                        run_id, expected_status="RUNNING",
                        new_status=state, lease_owner=owner,
                    )
                    run.finish_step(memory_step, error=str(exc))
                    legacy._fail(run, target_state, exc)
                    if not run.machine.state.value.startswith("FAILED"):
                        run.machine.fail(
                            AutopilotState.FAILED_PANEL_GENERATION,
                            reason=str(exc),
                        )
                    run.log_event(
                        "step_failed", {"step": hook_name, "status": state},
                    )
                    return run

            self.repository.transition_run(
                run_id, expected_status="RUNNING",
                new_status="COMPLETED", lease_owner=owner,
            )
            # Best-effort legacy files are supplemental; Work DB is authority.
            return legacy._finalize(run)
        finally:
            # Old process owners cannot release a newly reclaimed lease.
            try:
                self.repository.release_lease(
                    work_id=self.work_id, lease_owner=owner,
                )
            except WorkLeaseConflictError:
                pass


__all__ = [
    "DurableAutopilotOrchestrator",
    "InterruptedStepError",
    "NeedsAttentionStepError",
    "RetryableStepError",
]
