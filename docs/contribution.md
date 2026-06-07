# Contributing

Thanks for your interest in ComfyUI Manga Autopilot! The project is
maintained on a per-issue basis with squash-merged PRs.

## Issue-first

Every change starts as a GitHub issue. Look for an open issue you would
like to pick up, or open a new one describing the problem.

## Branch naming

```text
{issue_number}-{type}
```

where `{type}` is one of: `feature`, `bug`, `docs`, `refactor`, `chore`,
`test`. Example: `48-feature`.

## Commit format

```text
{type}: <short summary> #{issue_number}
```

Example: `feat: character service + Character Manager UI #47`.

## Pull requests

- One branch per issue
- Squash-merge with `--delete-branch --admin` once checks pass
- Title and body must reference the issue with `Closes #N` (or
  `Refs #N` if a follow-up)
- Make sure `pytest tests/backend/` and `ruff check .` are both clean

## Local checks

```bash
pip install -e ".[dev]"
pytest tests/backend/
ruff check .
```

## Style

- Python: ruff enforces import ordering and catches common bugs
  (`B`/`F`/`E` rules are enabled). Indent with 4 spaces.
- JavaScript: vanilla JS, no bundler. Mount UI components via
  `window.MangaAutopilot.mountXyz(root, opts)`.

## Releasing

- Bump version in `pyproject.toml`
- Add a CHANGELOG entry under the new version heading
- Tag and push the tag; the maintainer will draft the GitHub release

## Code of conduct

Be kind, leave the campsite cleaner than you found it. The
[GitHub community guidelines](https://docs.github.com/en/site-policy/github-terms/github-community-guidelines)
apply.
