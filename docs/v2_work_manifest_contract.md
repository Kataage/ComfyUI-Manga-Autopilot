# v2 Work manifest contract

This document mirrors the canonical v2 persistence decision for Work manifests.
The Work DB remains the Source of Truth for Work-local semantic state.

## Live mutable Work manifest

Canonical live Work manifests use `format_version = 2`:

```json
{
  "format": "manga-autopilot-work",
  "format_version": 2,
  "work_id": "work_...",
  "database": "work.sqlite3",
  "work_schema_version": 1,
  "created_at": "...",
  "app_version": "...",
  "integrity": {
    "mode": "live_mutable",
    "database_hash_policy": "package_only"
  }
}
```

A live `work.sqlite3` changes during legitimate editing. A persistent SHA-256 of
that mutable file is therefore not a live corruption signal. Live DB integrity is
checked with SQLite integrity/foreign-key validation.

`app_version` identifies the application version that most recently created or
normalized the live manifest. It is not semantic Work state.

## Immutable portable package manifest

A finalized `.mangaautopilot` package uses the same format identity/version,
but immutable package integrity:

```json
{
  "format": "manga-autopilot-work",
  "format_version": 2,
  "work_id": "work_...",
  "database": "work.sqlite3",
  "work_schema_version": 1,
  "created_at": "...",
  "app_version": "...",
  "integrity": {
    "mode": "immutable_package",
    "database_hash_policy": "sha256",
    "database_sha256": "..."
  }
}
```

`database_sha256` is calculated from the exact `work.sqlite3` bytes that are
included in the finalized package, after DB writes/checkpoint preparation is
complete and before archive finalization. Package verification compares those
exact packaged bytes against the digest.

`app_version` identifies the application version that wrote the package
manifest; it does not mean the Work originated with that version.

## Compatibility

- Format v2 is canonical for live and package manifests.
- Legacy format v1 is accepted only as compatibility input.
- A v1 live `database_sha256` is not enforced against a mutable live DB.
- After Work DB identity is validated, a v1 live manifest is normalized to v2.
- Unsupported future format versions are rejected rather than guessed.
- Manifest normalization does not mutate Work semantic state.

## Integrity boundary

Master `work_catalog.manifest_hash` may hash the live manifest file itself for
catalog/discovery bookkeeping. It is not a Work DB hash and does not replace
SQLite integrity checks.

Immutable package checksum verification and live SQLite integrity validation are
separate mechanisms.
