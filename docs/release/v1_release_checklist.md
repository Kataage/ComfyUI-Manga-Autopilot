# v1 Release Checklist

Use this checklist before creating a GitHub Release.

## Required

- [ ] `ruff check .` passes
- [ ] `pytest tests/backend/ -q` passes (500+ tests)
- [ ] `pytest -m release_gate -q` passes (6 key E2E tests)
- [ ] `CHANGELOG.md` updated
- [ ] `README.md` updated
- [ ] `README.ja.md` updated
- [ ] `docs/release/v1_acceptance_matrix.md` reflects current state
- [ ] Release notes drafted (`docs/release/v*_release_notes.md`)
- [ ] Version bumped in `pyproject.toml` and `__init__.py`

## Optional (manual)

- [ ] Real ComfyUI opt-in E2E passes
- [ ] Manual ZIP export / import smoke test
- [ ] Manual generated project re-edit smoke test
- [ ] `pip install -e .` succeeds from clean venv

## GitHub Release

- [ ] Create git tag (e.g. `v0.1.0-rc1`)
- [ ] Push tag: `git push origin v0.1.0-rc1`
- [ ] Create GitHub Release with release notes
- [ ] Attach sample workflow files if relevant
- [ ] Verify CI passes on the tagged commit
