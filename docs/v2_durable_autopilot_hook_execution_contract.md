# Durable Autopilot hook execution and omission receipts (Issue #371)

This document describes only the **opt-in Work-DB `DurableAutopilotOrchestrator` bridge**. It does not change legacy project HTTP routes and does not establish production manga quality, GPU or live ComfyUI qualification.

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
