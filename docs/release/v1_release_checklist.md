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

## Tag and release creation

### 1. Final verification on main

```bash
git checkout main
git pull
ruff check .
pytest tests/backend/ -q
pytest -m release_gate -q
```

### 2. Create and push tag

```bash
git tag v0.1.0-rc1
git push origin v0.1.0-rc1
```

### 3. Create GitHub Release (draft)

```bash
gh release create v0.1.0-rc1 \
  --title "v0.1.0-rc1 — Autopilot pipeline release candidate" \
  --notes-file docs/release/github_release_v0_1_0_rc1.md \
  --draft \
  --prerelease
```

### 4. Verify

- [ ] Tag appears on the correct commit on `main`
- [ ] CI passes on the tagged commit
- [ ] GitHub Release draft is created with correct body
- [ ] Release is marked as pre-release
- [ ] Attach sample workflow files if relevant
