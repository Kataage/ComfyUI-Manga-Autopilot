# Development Workflow

This document defines the branch model and release flow for ComfyUI Manga Autopilot.

## Branch roles

### `main`

`main` holds completed, release-ready code.

Normal Issue development does not merge directly into `main`.
Changes reach `main` when the integrated `develop` branch is ready for release.

`main` should remain:

- release-ready;
- free from ordinary in-progress development;
- representative of the current stable or release-prepared state.

### `develop`

`develop` is the integration branch for ongoing development.

Issue branches are created from `develop`, and completed Issue pull requests target `develop`.
Changes intended for the next release are integrated and verified here first.

## Issue branches

Create one work branch per Issue.

Naming convention:

```text
{issue-number}-{type}
```

Examples:

```text
218-bug
219-feature
120-docs
85-chore
```

The `type` is a short category describing the Issue.

Issue branches should normally start from `develop`.

## Normal development flow

```text
develop
  ↓
{issue-number}-{type}
  ↓
implementation / tests
  ↓
Pull Request
  ↓
develop
```

Rules:

- Use one work branch per Issue.
- Keep the branch focused on that Issue.
- Do not mix unrelated large changes into the same Issue branch.
- Pull requests for ordinary development target `develop`.
- Completed Issue work is integrated into `develop`.

## Release flow

When the next release is ready, verify the integrated state on `develop`, then merge it into `main` and publish the release from `main`.

```text
Issue branches
      ↓
   develop
      ↓
release verification
      ↓
    main
      ↓
   Release
```

Ordinary Issue branches should not bypass `develop` and merge directly into `main`.

## Summary

```text
main
  = completed / release branch

develop
  = ongoing development integration branch

{issue-number}-{type}
  = individual Issue work branch
```

Standard integration order:

```text
Issue
  ↓
{issue-number}-{type}
  ↓
develop
  ↓
main
  ↓
Release
```

## Existing work during migration

Existing pull requests and historical branches should be evaluated individually.

Do not rewrite or discard existing work merely to conform to the new naming convention.
Preserve validated implementation history and hardware-test evidence while moving active development toward the `develop`-based workflow.
