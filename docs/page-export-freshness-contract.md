# v2 Work Page PNG historical freshness contract (Issue #333)

## Source of truth

A Work PNG Artifact's immutable `status='READY'` means its file was
published and its hash/provenance recorded. READY **does not** mean that the
current Page still matches the saved PNG. Artifact rows and historical files
must not be rewritten when later Page editing occurs.

`GET /api/v2/works/{work_id}/exports` returns every non-archived READY
`page_render` PNG row with a separate read-model freshness field:

- `CURRENT` / `is_current=true`: registered, attributable to a Page render,
  owning Page/Layout present, and no later relevant committed source change.
- `STALE` / `is_current=false`: a later Page, Layout, Slot or Panel change
  invalidated the source; the Page was archived; or a new READY Candidate
  made an unpinned implicit Candidate selection ambiguous.
- `UNVERIFIED` / `is_current=false`: historical/manual Artifact lacks the
  v2 render fingerprint or its source Page/Layout is absent, so freshness
  cannot be positively established.

Additional response fields: `freshness_reason` and
`stale_since_commit_seq` (the earliest relevant post-publication
invalidation commit where applicable).

## Ordering rule

For each Artifact, compare its immutable `created_commit_seq` to the
`created_commit_seq` of `page_render` / `page_export` invalidations for
the owning Page, in **one SQLite read snapshot**. Any invalidation after the
Artifact's commit makes that Artifact historically stale forever. This holds
even if `invalidations.resolved_commit_seq` is later filled, as resolution
must not revalidate an older immutable image. A newer exported Artifact,
registered after a source edit under #335's final Work transaction guard,
is eligible independently of older Artifacts.

Current status is a read projection, not a DB migration or a mutable
Artifact lifecycle value. Editing another Page cannot stale unrelated
outputs. Repeated exports from unchanged source may both be CURRENT. This
classifier intentionally does not depend on legacy Project JSON.

When Panel selection is NULL and a sole READY panel Candidate is used by the
exporter, a later newly registered READY Candidate for that Panel also
invalidates the earlier implicit choice, even without a formal Panel update.
The exporter itself continues to require an unambiguous candidate at creation.

## Access and safety

Historical PNGs remain available at `GET /exports/{artifact_id}/png` with
their original hash verification and `X-Work-Export-Freshness` response
header. An explicit consumer can request `?require_current=1` to fail with
HTTP `409 stale_export` if the Artifact is STALE or UNVERIFIED at the
request's authoritative DB snapshot. The default historic download never
pretends that the file is newly rendered.

The HTTP freshness gate is a point-in-time test, **not** an atomic lease on
the image for the duration of network transfer. A future mutation occurring
after the read snapshot may change currentness; durable downstream workflows
must revalidate inside their own persistence/acceptance transaction.

Raw out-of-band SQLite tampering without formal domain invalidations is
outside the supported command contract. In older/pre-v2 Works with unknown
provenance, the classifier fails closed as UNVERIFIED rather than claiming
CURRENT based on READY alone. Archive visibility in the Page Editor and
active-render exclusion are owned by #337.

## Tests

The Linux full suite, Windows Work persistence/API suite, browser contract,
and live restarted-HTTP/Node integration cover initial CURRENT exports,
post-edit STALE history, a fresh new render, repeated unchanged exports,
unrelated Page edits, fresh-only 409, retained historic downloads, missing
provenance, late implicit Candidate changes and re-opened Work state.
