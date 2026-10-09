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

## Live Work lease release safety (Issue #382)

Normal `DurableRunRepository.release_lease` is deliberately **not**
an interruption or recovery operation. A Work lease bound to a Run may be
released by its authorized, unexpired owner only when the Run is no longer
`RUNNING` **and** no attached RunStep or RunStepAttempt remains `RUNNING`.
The Run, Steps and Attempt receipts are checked inside the very same
`BEGIN IMMEDIATE` transaction that deletes the Work lease. A missing or
foreign attached Run is also a release conflict. Unbound maintenance leases
and fully reconciled non-running Runs can be released normally.

If `DurableAutopilotOrchestrator.execute` exits with an exception or
cancellation **after** marking a Run `RUNNING`, and it has not durably
finalized or interrupted that state, its best-effort `finally` block must
not delete the Work lease. The Repository release guard rejects deletion,
preserving the old owner and Run ID as explicit recovery evidence. Without
a running heartbeat the lease eventually expires and remains a blocking
tombstone. A new owner must use the original owner token to reclaim the
**same Run**, call `recover_interrupted_run`, and explicitly reconcile
uncertain external side effects before resuming or releasing the lease.
An operator should not interpret an early error as successful completion.

The independent Run-transition consistency gap — the ability to mark a
Run terminal while a child receipt remains `RUNNING` — is separately
tracked as Issue #383. The release guard prevents such a malformed receipt
from silently losing its lease but does not fix the creation of that
inconsistency. The #374/#375/#378/#379 invariants are preserved.
No real GPU, live ComfyUI, installer or browser signoff is implied.

## Durable Run exit and child receipt consistency (Issue #383)

A durable Run must not leave `RUNNING` while any RunStep or RunStepAttempt
belonging to it is still `RUNNING`. Before `transition_run` accepts an
ordinary exit to `COMPLETED`, `PAUSED`, `FAILED_RETRYABLE`,
`FAILED_TERMINAL`, `INTERRUPTED`, `NEEDS_ATTENTION` or `CANCELLED`,
it validates **both** child receipt tables inside the same
`BEGIN IMMEDIATE` transaction as the Run UPDATE. A RunStep already marked
terminal can still contain a historically malformed in-flight Attempt,
so checking only current RunSteps is insufficient. Any unresolved child
raises `DurableRunStateError`, preserving original status, owner, heartbeat,
attempt provenance and the Work lease for explicit reconciliation.

Orchestrated stage success, retryable failure and task cancellation first
record a final Step **and Attempt** outcome under the live Work owner, then
transition their Run. The special `recover_interrupted_run` function
retains its privileged transactional crash-recovery path: with the
reclaimed owner's fresh Work lease, it atomically marks the old active
Step/Attempt receipts `INTERRUPTED` before marking the Run
`INTERRUPTED`. Unknown external side effects are not silently treated as
successful execution.

For legacy malformed snapshots in the test suite, direct fixture-only SQL
replicates a terminal Run with a `RUNNING` child; production
`transition_run` must never be used to manufacture this inconsistency.
The #382 live lease-release guard remains an independent fail-closed
second layer. The #379 expired cross-Run transfer guard remains unchanged.
Neither layer is a substitute for the #378 matching-owner Write fence.
No real GPU/ComfyUI/browser/installer or manga-quality acceptance is implied.

## Successful Run status requires successful registered child receipts (#386)

`DurableRunRepository.transition_run` treats `RUNNING -> COMPLETED` as
an assertion about every RunStep **already registered** for that parent:
each Step must be `COMPLETED`, and its latest durable RunStepAttempt
(`attempt_no == attempt_count`) must exist, be `COMPLETED`, and carry
the same input fingerprint as the current Step. The check is atomic with
the parent status UPDATE under the same `BEGIN IMMEDIATE` transaction;
a violation raises `DurableRunStateError` and does not rewrite Run,
Step, Attempt, or Work lease rows. A Run with no registered Steps can
still complete through the generic Repository contract; the opt-in
Autopilot orchestrator separately enforces required hooks and explicit
skeleton-mode omissions.

Older `FAILED_RETRYABLE` or `INTERRUPTED` attempts are **durable
history**, not current execution failures: a successful later attempt
makes the Step eligible for its parent's `COMPLETED` status. The
existing #383 check continues to deny *any* `RUNNING` Attempt,
including an erroneously active old attempt. A non-successful Run exit,
such as `FAILED_RETRYABLE`, `FAILED_TERMINAL`, `CANCELLED`, `PAUSED`,
`INTERRUPTED` or `NEEDS_ATTENTION`, is allowed to preserve registered
`PENDING` downstream Steps while obeying the existing no-running-child
rule, so interrupted flows remain explicitly recoverable.

This status reconciliation concerns **successful parent transition
semantics** and does not address the separate #387 contract for
post-terminal Step mutation; it is not authorization to edit completed
Run history, run hooks outside their leases, or suppress an external
effect awaiting reconciliation. No SQLite migration is required.
Real ComfyUI, GPU, browser, installer and manga-quality testing
remain separate from these GPU-free repository tests.

## Terminal RunStep history immutability (#387)

`DurableRunRepository` must not let a terminal parent Run (`COMPLETED`,
`FAILED_TERMINAL`, or `CANCELLED`) gain new RunSteps or rewrite any
recorded Step state, input fingerprint, output/attempt receipt, or Step
heartbeat. A finished Run remains final even after its Work lease is
normally released and the generic ownerless Repository API becomes
available. A current, valid Work mutation owner token does **not**
authorize reopening the final Run. Step creation, pending fingerprint
updates, completed-Step `STALE` invalidation, finishing a historically
inconsistent in-flight Step, and heartbeating that Step all check the
parent Run's terminal status **inside their existing `BEGIN IMMEDIATE`
write transaction**, in addition to the existing owner fence.

The existing `start_step` method separately requires parent `RUNNING`,
so it already denies starts against a terminal parent. Nonterminal
`PENDING`, `RUNNING`, `PAUSED`, `FAILED_RETRYABLE`, `INTERRUPTED`
and `NEEDS_ATTENTION` state handling retains its existing owner and
step-status contracts. A step may be invalidated while its parent Run
remains `RUNNING`; a later successful Attempt can legitimately
complete that parent (Issue #386), but a final Run can never be edited
retroactively. Historical corrupted terminal-parent/RUNNING-Step test
snapshots are fixture-only; do not manufacture those via production
`transition_run` (Issue #383).

This applies to GPU-free durable receipt and lease invariants, not a
sign-off of actual ComfyUI/GPU/browser output or rendering quality.

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
