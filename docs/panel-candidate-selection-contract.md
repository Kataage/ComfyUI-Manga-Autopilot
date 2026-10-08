# Selected Panel Candidate identity and ownership (Phase B / #334)

## Interim v2 Work contract

W0005 stores `panels.selected_candidate_id` as nullable TEXT; it does **not**
create a separate `panel_candidates` relation. For the current Phase B Work
slice, a **non-null selected_candidate_id is the ID of an immutable
`artifacts` row** in that same Work database. That row MUST have:

- `artifact_type = 'panel_candidate'`;
- `scope_type = 'panel'` and `scope_id = panels.id`;
- `status = 'READY'`, `archived_at IS NULL`;
- a supported export-image MIME type: `image/png`, `image/jpeg`, or `image/webp`.

Unselected `NULL` is permitted; no filename/path guess or cross-Work lookup
occurs during selection. A separate Candidate ID is **not** supported by
this temporary schema: callers must register the Panel-owned candidate image
Artifact first and supply **that Artifact's ID**, not a generation attempt,
job ID, or external candidate name. Any future normalized PanelCandidate
relation must explicitly migrate this relationship, not silently interpret
unresolved strings.

## Commit-time integrity

`PanelRepository.update_panel(panel_id, expected_revision=...,
selected_candidate_id=...)` checks its optimistic Panel revision and then
validates the Artifact row with **the same SQLite `BEGIN IMMEDIATE` Work
write transaction** that commits the Panel. An invalid selected image raises
`PageDomainCandidateSelectionError` (a `PageDomainOwnershipError`);
the complete command is rolled back, including simultaneous semantic changes,
with **no new commit, entity revision, or invalidation**. A stale optimistic
revision is reported first using the existing `RevisionConflictError`.

The public repository does not check file bytes or SHA-256 at selection time.
The exporting consumer still verifies immutable file presence, ownership,
content hash, MIME and correct Panel scope immediately before rendering. A
valid selection is not a promise that the file cannot later be tampered with.

## Historical Work compatibility

Prior application versions could already have stored missing or mis-owned
`selected_candidate_id` values. Opening these Works and making unrelated
Panel metadata edits is **not** blocked or silently repaired. Repeating a
historically invalid selection is rejected; the user/tool can explicitly clear
the selection to `NULL` or replace it with a registered, correctly owned
image Artifact under the current revision. The exporter continues to reject
those historical invalid references until repaired. The migration does not
rewrite old Work rows, change Artifact IDs, or introduce a Phase C dependency.

The invariant applies to the **PanelRepository command boundary**, not
out-of-band raw SQLite writes by external tools. Tests inject raw corrupt
rows only to prove the older-data export guard and the repair path.
