# Active/archive v2 Page and Panel policy (Phase B / Issue #337)

## Storage and authority

`pages.archived_at` and `panels.archived_at` in the Work SQLite database
determine active-vs-archived lifecycle for Page Editor and PNG export. An
archive is a revisioned **soft state change**: it must not delete the row,
LayoutSlots, linked Artifacts, output bytes, Work commit, or older revisions.
An explicit unarchive (set `archived_at=NULL` through the domain repository
with its current optimistic revision) returns an entity to the active view.

## Page Editor and Export Center

`GET /api/v2/works/{work_id}/pages` enumerates **active** Pages by default;
`?include_archived=1` opt-in includes history. The default
`GET /api/v2/works/{work_id}/pages/{page_id}` rejects an archived Page
with HTTP `409 page_archived` and excludes archived Panel rows on an
active Page. An explicit `?include_archived=1` allows **read-only**
retrieval of the archived Page and/or archived Panels with full persisted
IDs, revisions, state and Layout; it does not enable archived editing.

A Page Editor layout PATCH for an archived Page fails atomically with
`409 page_archived` inside `LayoutRepository.update_layout`'s
`BEGIN IMMEDIATE` transaction before a new Work commit or change to
Layout/Slots/Panel bindings. This guards against races after the browser
loads the active Page. Page/Panel archive and unarchive themselves remain
the responsibility of their revisioned domain repositories, not a new
client-side direct SQLite command.

An active Page Editor Layout PATCH with `panel_bindings` cannot update an
archived Panel, even if its ID and latest revision are known, or the requested
binding is a no-op. The domain repository checks `panel.archived_at` inside
the same `BEGIN IMMEDIATE` transaction **before** any Layout/Slot/Panel
writes or Work history commits and returns HTTP `409 panel_archived`.
This applies to mixed commands containing otherwise-valid Layout geometry,
Slot edits and bindings for active Panels: the entire command is rejected
without partial history or revision changes. After an explicit revision-guarded
Panel unarchive, that Panel can again participate in active binding updates.
This is the #344 closure of the read-model / command-model gap in #337.

## Atomic layout save acknowledgement (#350)

The successful `PATCH /pages/{page_id}/layout` response contains the persisted
Page, LayoutInstance, LayoutSlots and **active Panels** as they stood **within
the same `BEGIN IMMEDIATE` transaction that accepted the save**. The
repository validates ownership, Page/Panel archived status, expected Layout/
Slot/Panel revisions, then applies the batch and reads the resulting Page
projection while it still owns the write lock. A no-op command produces that
same transaction-owned projection without creating an extra Work commit.
Repository clients that request only the Layout row retain their previous
return-value contract.

An independent writer can archive the Page or edit Layout/Slots/Panel bindings
immediately *after* that commit and *before* the PATCH response reaches the
browser. Such a later change must not retroactively turn an accepted save
into HTTP `409 page_archived` or `409 revision_conflict`; nor may its
newer state leak into the prior save's success acknowledgement. A subsequent
`GET` may correctly show the later change (or reject an archived Page).
Conversely, an archive or revision conflict encountered **under the original
write lock before mutation** still returns the existing explicit HTTP 409,
with no partial batch or new Work history.

The response snapshot is fully materialized before transaction commit: a
projection/decode failure rolls the write back instead of causing a
post-commit failure. This preserves the #344 archive guards and revision/
invalidation provenance without importing legacy project JSON.

Both Page Editor and Export Center rely on the same active Page list
endpoint for selectors, so archived Pages are never silently offered as
ordinary editable/exportable Pages. Historical exports remain visible
through the independent Export Center Artifact listing.

## PNG rendering

The exporter obtains one *full* persisted Page projection (including
archived Panels) for optimistic rendering/revision fingerprint checks.
A Page with `archived_at != NULL` is refused with HTTP
`422 export_precondition_failed` before rendering. Only Panels with
`archived_at IS NULL` contribute compositing inputs, Candidate verification,
input budgets and the final Artifact dependency list; archived Panel image
files need not exist to export an otherwise viable active Page. If every
Panel is archived, exporting a misleading blank Page is rejected with
`422`. No legacy JSON fallback occurs.

A full-source-state snapshot is preserved for the final #335
`BEGIN IMMEDIATE` Artifact registration guard, so archiving a Page or
Panel concurrently with rendering/publication causes a
`409 page_changed` conflict before any READY Page render is registered.
Safe published-orphan-file crash semantics are unchanged.

New Page renders reflect active content; historical PNGs remain immutable.
The #333 Export Center freshness classifier determines which old renders
have become STALE after archival revisions, without modifying prior
Artifact rows, bytes or hashes. Unarchiving does not revive an older
stale PNG: a fresh render is required to obtain CURRENT source provenance.

## Verification and limits

Linux and Windows HTTP/persistence suites cover archived Page selectors,
opt-in history, archived Panel filtering, transactional layout PATCH
rejection, empty active Panel refusal, byte-for-byte preserved old output,
unarchive/reopen, and concurrent archive after immutable file publication.

This does **not** change the broader semantics of other Work domain
repositories, or claim complete browser/real GPU coverage. A separate
Phase B closure audit is required after merging this final strict
hardening issue.
