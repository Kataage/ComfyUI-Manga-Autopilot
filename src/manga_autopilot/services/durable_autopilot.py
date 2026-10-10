"""Opt-in v2 Work-DB adapter for the existing Autopilot pipeline (#246).

Legacy project routes keep their existing behavior. This adapter runs the
same OrchestratorHooks against Work DB-authoritative Runs and RunSteps.
Inputs and hook outputs must be JSON-serializable; failing to persist the
result is a failed step, never an untracked completion.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
from collections.abc import Callable, Mapping
from contextlib import suppress
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
)
from manga_autopilot.storage.repository import owned_work_mutation


class RetryableStepError(RuntimeError):
    """The hook is explicitly safe to retry after inspecting its outcome."""


class NeedsAttentionStepError(RuntimeError):
    """The hook requires human reconciliation before being resumed."""


class InterruptedStepError(RuntimeError):
    """The hook reports an interrupted or uncertain operation."""


class DurableHeartbeatLostError(WorkLeaseConflictError):
    """The background Work lease heartbeat is no longer protecting this Run."""


def _check_heartbeat(task: asyncio.Task[None]) -> None:
    """Never begin or acknowledge an effect after its lease guardian exits."""
    if not task.done():
        return
    if task.cancelled():
        raise DurableHeartbeatLostError("durable Work lease heartbeat was cancelled")
    try:
        task.result()
    except Exception as exc:
        raise DurableHeartbeatLostError(
            "durable Work lease heartbeat failed; reconcile any external effects"
        ) from exc
    raise DurableHeartbeatLostError("durable Work lease heartbeat stopped unexpectedly")


async def _invoke_guarded_hook(
    hook: Any, run: AutopilotRun, heartbeat_task: asyncio.Task[None],
) -> Any:
    """Race the hook against lease loss, then drain every owned local worker.

    An asyncio cancellation cannot stop a synchronous ComfyUI/Pillow hook.
    The parent still holds its Work lease and must never detach the child task.
    On lost ownership its durable results are NOT persisted as successful.
    """
    _check_heartbeat(heartbeat_task)
    hook_task = asyncio.create_task(_invoke_durable_hook(hook, run))
    try:
        await asyncio.wait(
            {hook_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        # Lease loss wins even if hook and guardian finished in the same tick.
        _check_heartbeat(heartbeat_task)
        return await hook_task
    except (Exception, asyncio.CancelledError):
        if not hook_task.done():
            hook_task.cancel()
        # Cancellation of the parent may repeat during the drain; the
        # to_thread hook still owns its OS worker until it actually exits.
        while not hook_task.done():
            try:
                await asyncio.shield(hook_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not hook_task.cancelled():
            try:
                hook_task.result()
            except BaseException:
                pass  # The original failure/cancellation owns this outcome.
        raise


async def _run_owned_durable_sqlite(
    operation: Callable[..., Any], *args: Any, **kwargs: Any,
) -> Any:
    """Keep Work SQLite connections and lease transactions in owned threads.

    Each repository method opens/closes its own SQLite connection in this
    worker. Cancellation does not terminate to_thread's OS worker, so the
    parent must drain the operation (including commit/rollback) before it
    can interrupt a Step or release the exclusive Work lease. ContextVars
    are copied by to_thread, preserving trusted Work-mutation ownership.
    """
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue  # Repeated cancellation cannot orphan SQLite I/O.
            except Exception:
                break
        if not worker.cancelled():
            try:
                worker.result()
            except BaseException:
                pass  # The original cancellation takes precedence.
        raise


def _json_value(value: Any) -> Any:
    """Use only data that survives a new process and exact JSON replay."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.loads(canonical_json(value))


async def _invoke_durable_hook(hook: Any, run: AutopilotRun) -> Any:
    """Run synchronous rendering/generation hooks off the event loop.

    Lease renewal must continue even while synchronous Pillow or filesystem
    hooks execute. Awaitable hook results are awaited on the calling loop.
    A cancelled synchronous invocation is drained before returning cancellation;
    the enclosing RunStep/Work lease remains RUNNING until the OS thread exits.
    """
    if hook is None:
        return None
    if inspect.iscoroutinefunction(hook):
        return await hook(run)
    # Cancelling an asyncio.to_thread await does not stop the OS thread.
    # Keep ownership of the worker until it exits: execute() must not mark
    # the RunStep INTERRUPTED or release its Work lease while synchronous
    # rendering/generation side effects may still be in progress.
    worker = asyncio.create_task(asyncio.to_thread(hook, run))
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        # The outer execute() coroutine can be cancelled repeatedly while
        # a hook is blocked. Keep draining under shield across *every*
        # cancellation, leaving its heartbeat and Work lease active.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                # A failed thread is finished; the caller still receives
                # cancellation, not a misleading hook failure.
                break
        if not worker.cancelled():
            # Observe late hook exceptions rather than leaking "Task
            # exception was never retrieved" during cancellation.
            try:
                worker.result()
            except BaseException:
                pass
        raise
    if inspect.isawaitable(result):
        return await result
    return result


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
    # Explicit GPU-free skeleton mode. An absent hook is NEVER silently
    # counted as execution unless the caller deliberately opts in here.
    allow_omitted_hooks: bool = False

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
        # The guardian ticks at least once per second. Smaller TTLs expire
        # on or before their first renewal, so fail before acquiring a lease.
        if type(self.lease_ttl_seconds) is not int or self.lease_ttl_seconds < 3:
            raise ValueError("lease_ttl_seconds must be an integer >= 3")
        if not isinstance(input_payload, Mapping) or not isinstance(step_inputs, Mapping):
            raise TypeError("durable inputs must be mappings")
        if type(self.allow_omitted_hooks) is not bool:
            raise TypeError("allow_omitted_hooks must be an explicit boolean")
        for name in _STEP_NAMES.values():
            hook = getattr(self.hooks, name, None)
            if hook is not None and not callable(hook):
                raise TypeError(f"durable {name} hook must be callable")
        missing = [
            name for name in _STEP_NAMES.values()
            if getattr(self.hooks, name, None) is None
        ]
        if missing and not self.allow_omitted_hooks:
            raise DurableRunStateError(
                "required durable stage hook is missing: "
                + ", ".join(missing)
                + "; explicitly opt into allow_omitted_hooks for skeletal tests"
            )
        durable = await _run_owned_durable_sqlite(self.repository.get_run, run_id)
        if durable["scope_type"] != "WORK" or durable["scope_id"] != self.work_id:
            raise DurableRunStateError("Run is not owned by this Work")
        if durable["run_kind"] != "AUTOPILOT":
            raise DurableRunStateError(
                "Durable Autopilot requires run_kind='AUTOPILOT'"
            )
        if durable["status"] in {"COMPLETED", "FAILED_TERMINAL", "CANCELLED"}:
            raise DurableRunStateError("terminal Run cannot resume; create a new Run")
        if durable["status"] == "NEEDS_ATTENTION" and not approve_needs_attention_retry:
            raise DurableRunStateError("Run requires explicit human reconciliation")
        if durable["status"] == "INTERRUPTED" and not approve_interrupted_retry:
            raise DurableRunStateError("interrupted Run requires explicit reconciliation")

        async def assert_completed_stage_provenance() -> None:
            # Never infer that old completed-null receipts executed a hook.
            # A changed hook set requires a fresh Run rather than silently
            # replaying already-completed stochastic descendants.
            for old_step in await _run_owned_durable_sqlite(self.repository.list_steps, run_id):
                if old_step["status"] != "COMPLETED":
                    continue
                old_output = json.loads(old_step["output_json"])
                old_mode = old_output.get("execution")
                available = (
                    getattr(self.hooks, str(old_step["step_key"]), None) is not None
                )
                if old_mode == "OMITTED" and available:
                    raise DurableRunStateError(
                        f"previously omitted stage {old_step['step_key']} "
                        "gained a real hook; create a new Run"
                    )
                if old_mode == "EXECUTED" and not available:
                    raise DurableRunStateError(
                        f"previously executed stage {old_step['step_key']} "
                        "lost its hook; create a new Run"
                    )
                if old_mode is None and old_output.get("value") is None:
                    raise DurableRunStateError(
                        f"ambiguous legacy null receipt at {old_step['step_key']}; "
                        "create a new Run rather than assuming a hook executed"
                    )

        # Preflight before acquiring a lease, then recheck after ownership is
        # acquired: a former owner may finish a stage between those moments.
        await assert_completed_stage_provenance()

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
        await _run_owned_durable_sqlite(self.repository.acquire_lease, 
            work_id=self.work_id,
            lease_owner=owner,
            lease_kind="AUTOPILOT_MUTATION",
            ttl_seconds=self.lease_ttl_seconds,
            run_id=run_id,
            reclaim_expired_owner=reclaim_expired_owner,
        )
        heartbeat_task: asyncio.Task[None] | None = None
        active_step_id: str | None = None

        async def renew_lease() -> None:
            # Running ComfyUI hooks can exceed one lease TTL. Refresh the
            # Work-exclusive token, Run and in-flight RunStep in each cycle.
            # All assignments to active_step_id happen on this event loop,
            # and each repository call uses its own short transaction.
            period = max(1, min(60, self.lease_ttl_seconds // 3))
            while True:
                await asyncio.sleep(period)
                try:
                    await _run_owned_durable_sqlite(self.repository.heartbeat_lease, 
                        work_id=self.work_id, lease_owner=owner,
                        ttl_seconds=self.lease_ttl_seconds,
                    )
                    await _run_owned_durable_sqlite(self.repository.heartbeat_run, run_id, lease_owner=owner)
                    if active_step_id is not None:
                        await _run_owned_durable_sqlite(self.repository.heartbeat_step, 
                            active_step_id, lease_owner=owner,
                        )
                except Exception as exc:
                    raise DurableHeartbeatLostError(
                        "durable Work lease heartbeat renewal failed"
                    ) from exc

        try:
            await assert_completed_stage_provenance()
            durable = await _run_owned_durable_sqlite(self.repository.get_run, run_id)
            if durable["status"] == "RUNNING":
                await _run_owned_durable_sqlite(self.repository.recover_interrupted_run, 
                    run_id, lease_owner=owner,
                )
                durable = await _run_owned_durable_sqlite(self.repository.get_run, run_id)
                if not approve_interrupted_retry and any(
                    step["status"] == "INTERRUPTED"
                    for step in await _run_owned_durable_sqlite(self.repository.list_steps, run_id)
                ):
                    raise DurableRunStateError(
                        "crashed in-flight step needs explicit reconciliation"
                    )
            await _run_owned_durable_sqlite(self.repository.transition_run, 
                run_id, expected_status=str(durable["status"]),
                new_status="RUNNING", lease_owner=owner,
            )
            heartbeat_task = asyncio.create_task(
                renew_lease(), name=f"durable-autopilot-heartbeat-{run_id}",
            )
            upstream = input_fingerprint({
                "run_kind": durable["run_kind"],
                "run_input": base_inputs,
            })
            current_steps = {
                step["step_key"]: step
                for step in await _run_owned_durable_sqlite(self.repository.list_steps, run_id)
            }
            for target_state, hook_name in _STEP_NAMES.items():
                _check_heartbeat(heartbeat_task)
                hook = getattr(self.hooks, hook_name, None)
                omitted = hook is None
                payload = {
                    "step_key": hook_name,
                    "stage_input": stage_inputs.get(hook_name),
                    "upstream": upstream,
                }
                # Keep the old executed-stage fingerprint stable for safe
                # process replay; explicitly omitted stages use a distinct
                # fingerprint so they cannot alias real execution.
                if omitted:
                    payload["execution"] = "OMITTED"
                fingerprint = input_fingerprint(payload)
                upstream = fingerprint
                step = current_steps.get(hook_name)
                if step is None:
                    step = await _run_owned_durable_sqlite(self.repository.create_step, 
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
                    await _run_owned_durable_sqlite(self.repository.mark_step_stale, 
                        step["id"], new_fingerprint=fingerprint,
                        lease_owner=owner,
                    )
                elif step["status"] == "PENDING" and (
                    step["input_fingerprint"] != fingerprint
                ):
                    await _run_owned_durable_sqlite(self.repository.set_pending_fingerprint, 
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
                await _run_owned_durable_sqlite(self.repository.start_step, 
                    step["id"], input_fingerprint=fingerprint,
                    lease_owner=owner,
                )
                active_step_id = str(step["id"])
                machine.advance(reason=hook_name)
                memory_step = run.record_step(hook_name, target_state)
                run.log_event("step_started", {"step": hook_name})
                try:
                    # Hooks may commit Work Page/Panel/Artifacts. Bind only
                    # this task's trusted durable owner; asyncio.to_thread
                    # copies the context, and the final DB transaction still
                    # fences expired/stale leases. HTTP tasks inherit none.
                    with owned_work_mutation(self.work_id, owner):
                        result = await _invoke_guarded_hook(
                            hook, run, heartbeat_task,
                        )
                    _check_heartbeat(heartbeat_task)
                    replayable = _json_value(result)
                    if hook_name == "finalize":
                        # Project-root report/mirroring performs potentially
                        # long, destructive filesystem copies. It is part of
                        # the final tracked RunStep, NOT post-COMPLETED cleanup:
                        # keep the Work/Run/Step lease guardian alive while the
                        # sync worker runs and drain it on cancellation/loss.
                        # A process crash or lost lease leaves this attempt
                        # RUNNING/INTERRUPTED for explicit reconciliation.
                        with owned_work_mutation(self.work_id, owner):
                            await _invoke_guarded_hook(
                                legacy._finalize, run, heartbeat_task,
                            )
                        _check_heartbeat(heartbeat_task)
                    await _run_owned_durable_sqlite(self.repository.finish_step, 
                        step["id"], status="COMPLETED",
                        output={
                            "value": replayable,
                            "execution": "OMITTED" if omitted else "EXECUTED",
                        },
                        lease_owner=owner,
                    )
                    run.finish_step(memory_step)
                    if replayable is not None:
                        run.store(hook_name, replayable)
                    if omitted:
                        run.log_event(
                            "step_omitted",
                            {"step": hook_name, "reason": "explicit_skeletal_mode"},
                        )
                    else:
                        run.log_event("step_finished", {"step": hook_name})
                except DurableHeartbeatLostError:
                    # If this owner still has a valid lease, record an
                    # INTERRUPTED attempt before releasing it. If the lease
                    # already expired or was reclaimed, only the new owner
                    # may reconcile the still-RUNNING attempt.
                    try:
                        await _run_owned_durable_sqlite(self.repository.finish_step, 
                            step["id"], status="INTERRUPTED",
                            error={"reason": "lease_heartbeat_lost"},
                            lease_owner=owner,
                        )
                        await _run_owned_durable_sqlite(self.repository.transition_run, 
                            run_id, expected_status="RUNNING",
                            new_status="INTERRUPTED", lease_owner=owner,
                        )
                    except (WorkLeaseConflictError, DurableRunStateError):
                        pass
                    raise
                except asyncio.CancelledError:
                    await _run_owned_durable_sqlite(self.repository.finish_step, 
                        step["id"], status="INTERRUPTED",
                        error={"reason": "task_cancelled"},
                        lease_owner=owner,
                    )
                    await _run_owned_durable_sqlite(self.repository.transition_run, 
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
                    await _run_owned_durable_sqlite(self.repository.finish_step, 
                        step["id"], status=state, error=error,
                        lease_owner=owner,
                    )
                    await _run_owned_durable_sqlite(self.repository.transition_run, 
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
                finally:
                    # Keep the active Step visible to renew_lease() throughout
                    # cancellation/guardian-loss worker draining. Clear it
                    # only after the Step receipt is finished or recovery
                    # leaves this owner; never heartbeat a terminal Step.
                    active_step_id = None

            _check_heartbeat(heartbeat_task)
            await _run_owned_durable_sqlite(self.repository.transition_run, 
                run_id, expected_status="RUNNING",
                new_status="COMPLETED", lease_owner=owner,
            )
            # Finalization already ran under the live lease, as the last
            # durable RunStep. Never run filesystem mutation after COMPLETED.
            return run
        except DurableHeartbeatLostError:
            # Lease failure can also be noticed between stages, with no
            # active Step. The Run must not be left as a normal success.
            try:
                await _run_owned_durable_sqlite(self.repository.transition_run, 
                    run_id, expected_status="RUNNING",
                    new_status="INTERRUPTED", lease_owner=owner,
                )
            except (WorkLeaseConflictError, DurableRunStateError):
                pass
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError, DurableHeartbeatLostError):
                    await heartbeat_task
            # Old process owners cannot release a newly reclaimed lease.
            try:
                await _run_owned_durable_sqlite(self.repository.release_lease, 
                    work_id=self.work_id, lease_owner=owner,
                )
            except WorkLeaseConflictError:
                pass


__all__ = [
    "DurableHeartbeatLostError",
    "DurableAutopilotOrchestrator",
    "InterruptedStepError",
    "NeedsAttentionStepError",
    "RetryableStepError",
]
