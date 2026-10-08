# Page PNG publication consistency (Phase B / #335)

## Authority and commit rule

A Page PNG is rendered from one persisted Work snapshot containing Page,
LayoutInstance, LayoutSlots, and Panels (including selected Candidate IDs).
The Page application projection used to render is also used by the commit-time
guard; the export request never trusts a browser-supplied layout.

Rendering and file validation are deliberately outside a SQLite write
transaction. The exporter performs a fast-fail read after rendering, but this
check alone is **not** sufficient: a legal Work edit can commit while the
ArtifactRepository copies and publishes the rendered file.

The final `page_render` registration therefore passes a DB-only
`commit_guard` to `ArtifactRepository.register_local_file`. After the file
has been fsynced and exclusively published, the repository starts its normal
`BEGIN IMMEDIATE` Work write transaction and invokes that guard **before**
creating the Work commit, Artifact row, or entity revision. The guard reads
the same authoritative Page/Layout/Slot/Panel projection from that existing
connection and compares its canonical fingerprint to the original snapshot.

- Equal snapshots: register a `READY` Artifact with immutable file hash,
  dependency fingerprint, Work commit, and revision in that transaction.
- Changed or missing Page: roll back the complete Artifact registration,
  report `PageExportConflictError` / HTTP `409 page_changed`, and require
  a fresh export. No misleading `201`, `READY` row, or Artifact revision
  is produced.
- Changes after the committed registration are later Work history: they do
  not rewrite the historical Artifact. Current/stale listing behavior is
  separately owned by #333.

The write lock serializes semantic edits and publication commits. A mutation
committed just before the guard is detected; a concurrent mutation attempted
after the guard must wait until registration has committed. A Page render
cannot be published as fresh from an already-superseded Work snapshot.

## Filesystem recovery

The filesystem and SQLite cannot be committed atomically. Preserve the
existing safety order: write and fsync temporary data, publish the immutable
final file exclusively, then commit the DB row. A guard rejection may leave
an unregistered final PNG (orphan) for later maintenance/recovery; **never**
remove, overwrite, or reuse a file from an existing registered Artifact.
This is preferable to committing an Artifact row that points to a missing
or incorrect file.

`commit_guard` is an internal, short, DB-only validation hook. It must not
open a second transaction, write to the Work, or perform filesystem/network
operations while the Work write lock is held.

## Tests and boundaries

Linux full-suite and Windows persistence tests cover edits during render,
after the fast-fail read but before Artifact copying, and after final file
publication but before DB commit. They verify a conflict response, absence
of an Artifact commit/revision/READY row, immutable orphan behavior, and
successful fresh retry.

No Work schema migration or Phase C dependency is introduced. Archived Page
and Panel active-render policy is independently tracked by #337.
