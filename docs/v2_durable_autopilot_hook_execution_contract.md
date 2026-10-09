# Durable Autopilot hook execution and omission receipts (Issue #371)

This document describes only the **opt-in Work-DB `DurableAutopilotOrchestrator` bridge**. It does not change legacy project HTTP routes and does not establish production manga quality, GPU or live ComfyUI qualification.

## Durable Run kind boundary

`DurableRunRepository.create_run` is intentionally generic: Work-scoped
`AUTOPILOT`, `EXPORT`, `QA`, and future operation-specific `run_kind`
values may be persisted. A caller must dispatch each kind to its appropriate
operation handler; this document does not define or implement other handlers.

The opt-in `DurableAutopilotOrchestrator.execute()` accepts **only** an
existing Run with `scope_type == "WORK"`, matching `scope_id`, and
**exact** `run_kind == "AUTOPILOT"` (case-sensitive). A foreign kind is
rejected with `DurableRunStateError` before acquiring the exclusive Work
mutation lease, before transitioning the Run, before creating any RunSteps or
attempts, and before invoking a stage hook. Neither the explicitly opted-in
GPU-free skeleton nor a process restart overrides this boundary.

This kind validation does not modify the existing AUTOPILOT stage fingerprint
or receipt policy. No migration or narrowing of the generic repository API is
required.

## Durable Run and RunStep ownership fence (Issue #378)

Every v2 Work database has a single logical mutation writer. The generic
`DurableRunRepository` intentionally allows unleased low-level Run and
RunStep setup/testing operations when **no Work lease exists**; ordinary
`AUTOPILOT`, `EXPORT` and `QA` Run kinds remain representable. It
does **not** interpret an omitted `lease_owner` as authorization to update a
Run or RunStep while another writer owns a Work mutation lease.

Within the same `BEGIN IMMEDIATE` write transaction as each mutation:

- Creating a new Run without an owner is refused if an active **or expired**
  Work lease row is present. An expired lease is a blocking tombstone until
  explicit, matching-owner recovery.
- All Run state transitions and RunStep create, input-fingerprint update,
  start/retry, stale-mark and finish/attempt receipt mutations require an
  owner token matching the live Work lease and current Run. Even a separate
  Repository instance pointing at the same DB cannot supply a wrong or
  absent token, forge a successful receipt, or bypass the lease.
- A Run still marked with a prior `lease_owner` is never silently editable
  as an unowned Run after its lease is gone.
- Read-only Run/Step/Attempt inspection does not require a writer token.

This fence is independent of the Autopilot Run-kind guard (#375), hook output
provenance (#371), Page/Artifact Work-commit lease gate (#374), and the
separate cross-Run expired-lease recovery issue (#379). It does not certify
real ComfyUI/GPU execution or provide a new HTTP orchestration API.

## Expired lease recovery across Runs (Issue #379)

An expired Work lease is a recovery tombstone: a different owner must
present the **exact old lease owner token** and rotate the owner. A new
Run may not inherit an expired lease while the old bound Run is still
`RUNNING`, even if the previous owner is known and the new Run has
`PENDING` status.

The supported crash-recovery sequence is:

1. Reacquire the expired lease **for the same Run ID** with the matching
   old-owner token and a *new* lease-owner token.
2. Call `recover_interrupted_run` while holding that new lease. The
   old Run becomes `INTERRUPTED`, in-flight Steps and Attempts become
   `INTERRUPTED`, and completed historical receipts remain unchanged.
   Uncertain external side effects require explicit human reconciliation.
3. Resume the interrupted Run with the approved retry policy, **or**
   release the recovered lease, then acquire a new lease for another Run.

The cross-Run handover check is in the same `BEGIN IMMEDIATE` transaction
as the lease replacement. It also refuses handover when an earlier Run is
already terminal but still contains a `RUNNING` Step or Attempt. A prior
Run already fully reconciled/terminal with no in-flight receipts, or an
expired legacy unbound maintenance lease, may be explicitly transferred.
The incoming and previous attached Runs must both match the Work scope.
Releasing/recovering leases never silently changes old Run or Attempt
history.

This is independent of the no-owner mutation fence (#378) and the exact
`AUTOPILOT` dispatch guard (#375). No real ComfyUI/GPU/browser acceptance
is implied.

## Supported, required, and explicitly omitted stages

- **Supported/executed:** a callable hook is provided for a stage in `OrchestratorHooks`. Its result (including a genuine JSON `null`) is serialized with an explicit `execution: "EXECUTED"` provenance marker in the completed RunStep/attempt receipt.
- **Required and absent (default):** `allow_omitted_hooks=False`. If any stage in the ordered Autopilot pipeline lacks a callable hook, `execute()` rejects the request before acquiring a Work lease or mutating a Run. This is the **required** mode for a production-intended invocation; a `COMPLETED` Run cannot be manufactured from absent generation/render/export hooks.
- **Intentionally disabled/omitted (opt-in GPU-free skeleton):** a caller explicitly sets `allow_omitted_hooks=True` and sets that stage's hook to `None`. Only then may the skeleton process the stage without calling the user hook. The RunStep/attempt receipt has `execution: "OMITTED"` and `value: null`, and an in-memory `step_omitted` event is logged. This does **not** mean a page or image was generated.
- A test/simulation may mix supported hooks and explicit omissions in one Run. The mode is **not** inferred from a missing hook. A non-callable, non-`None` hook is invalid. The separate built-in, best-effort legacy artifact mirroring still executes inside the final tracked stage under the active Work lease (#370), even if the user-supplied `finalize` hook is intentionally omitted.

### Persisted status constraint

The existing Work SQLite contract has no `SKIPPED` RunStep status. To avoid an incompatible schema migration, explicit skeleton omission remains a terminal `COMPLETED` step with a **distinct, mandatory output provenance marker**. Inspect `run_steps.output_json.execution` (or the matching `run_step_attempts.output_json`) before treating a skeleton's `COMPLETED` status as proof of stage execution. A `Run.COMPLETED` containing omitted receipts is a **completed skeleton orchestration**, **not** a fully generated production manga.

## Fingerprint stability and restart safety

Executed-stage fingerprints are deliberately **unchanged**, protecting valid persisted stochastic work on normal process restart. An explicitly omitted stage adds `execution: "OMITTED"` to its fingerprint input so it cannot alias an actually supported stage with the same inputs.

Before lease acquisition and again **after acquiring the exclusive Work lease**, a resumed Run checks all previously completed stage receipts. Changing an `OMITTED` stage into a callable hook, or removing a hook from a previously `EXECUTED` stage, is rejected with a request to **create a new Run**. It is not silently skipped and cannot automatically replay downstream stochastic effects. Older `COMPLETED` receipts with `value: null` and no execution marker are ambiguous: even an originally present hook may have returned null. They are rejected for explicit new-Run reconciliation instead of being silently trusted.

The caller still owns meaningful stage-input/capability versioning in `step_inputs`. Replacing a callable implementation with another callable implementation cannot be inferred reliably from a Python function object; the caller must version those inputs and follow existing fingerprint/reconciliation rules. A hard process kill or lost Work lease still requires explicit recovery of uncertain external side effects; these receipts cannot forcibly stop remote ComfyUI/GPU work.

## Tests and scope

GPU-free tests cover default fail-closed validation (no lease/Run mutation), a real async hook returning null distinguished from absent hooks, fresh Repository attaching an async hook after a deliberate omission and refusing unsafe resume, deliberately creating a new Run, and ambiguous legacy null receipts. The prior isolated v2 hard-kill subprocess fixture explicitly opts into skeletal mode for its intentionally unimplemented stages. No live GPU/ComfyUI or real production end-to-end assertions are made by these tests.
