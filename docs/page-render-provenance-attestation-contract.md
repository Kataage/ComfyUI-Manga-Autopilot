# Work Page render provenance attestation v1 (Issue #343)

## Authority and threat boundary

`Artifact.status='READY'`, a valid SHA256 file hash, and a 64-character
`dependency_fingerprint` prove neither currentness nor that the registered
bytes came from a saved Work Page. Generic Work Artifact import, recovery and
`register_local_bytes/file` intentionally support arbitrary media and
fingerprints. **None of those APIs can attest an authoritative Page render**,
even if passed a caller-chosen `commit_guard`.

Only `WorkPageExportService` may use the private
`ArtifactRepository._register_verified_page_render_file` pathway after
reading the complete persisted Page/Layout/Slot/Panel snapshot, resolving
the selected current Panel-owned Candidate images, rendering with explicit
settings, and computing its canonical `dependency_fingerprint`. During
the final `BEGIN IMMEDIATE` Work transaction the repository **also**
independently verifies the current Page snapshot digest and active
Panel/Candidate identities, revisions, source hashes and ownership; #335's
detailed guard additionally rechecks candidate ambiguity and integrity.
Any mismatch aborts before a READY Artifact row or new Work commit is added.
An already published file may remain as a safe, unregistered orphan.

## Atomic immutable proof

A successful guarded export creates exactly one Work `commits` row with:

- `operation_type = register_verified_page_render_v1`
- `actor_type = system`
- `reason` = canonical JSON source-provenance envelope (v1), containing:
  `attestation_version=1`, the exact Artifact ID, Page ID, resulting SHA256
  `dependency_fingerprint`, and the actual exporter
  `source_fingerprint_payload`.

The payload includes `page_id`, `page_state_sha256` (entire Page/Layout/
Slots/**including archived Panels**), each active Panel ID/revision,
LayoutSlot ID/revision, selected immutable Candidate Artifact ID/content
SHA256, background, outer-border flag, export profile, and renderer kind.
`dependency_fingerprint` is SHA256(canonical JSON of this payload).
The same Work transaction inserts the linked READY `artifacts` row with
`created_commit_seq` equal to this commit, and its canonical first entity
revision. No W000x migration or external credentials are introduced;
the existing append-only commit reason field stores the proof.

`page_png_freshness` must use the same SQLite snapshot to load this exact
commit and validate operation, actor, canonical envelope, linkage to the
Artifact ID, Page ID and fingerprint, and recomputed payload SHA256.
Only then may the existing #333 history/invalidation and #337 archived Page
checks return `CURRENT`. A forged-looking SHA256 on a generic import or
an old row without this proof is **UNVERIFIED** even if its PNG file has
the correct hash. Existing invalidation chronology still marks attested
old outputs `STALE` after source edits.

## Compatibility and limits

Historical Artifacts, including genuine PNGs published **before** this
attestation protocol, retain their immutable rows, verified file downloads
and original hashes but are conservatively **UNVERIFIED** for active freshness
because they lack enforceable proof. The Export Center lists them, but
`?require_current=1` returns a structured HTTP 409. Re-export to establish
fresh provenance; do **not** backfill or guess attestations from a string
or rewrite old history.

This is application-managed provenance, not cryptographic proof against
arbitrary direct SQL/database tampering by a process already authorized to
modify Work DB internals. The renderer is trusted to use the specified
inputs; we do not re-render the image inside a SQLite write transaction.
File integrity is independently checked by the existing streamed SHA256
download and #336 memory/IO budgets. GPU/real-ComfyUI graphical-browser
inference testing remains a separate project acceptance gate.
