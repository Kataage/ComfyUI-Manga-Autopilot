# Project Examples

Sample generated project ZIPs will be added here in a future release.

## Current state

- Project portability via ZIP bundle export / import is verified by
  `test_project_bundle_import_e2e.py`.
- Projects can be generated locally using the Autopilot pipeline
  (see the release gate and E2E tests).
- No large binary files (images, ZIPs) are committed to this
  directory yet.

## Generating a sample project

To create a sample project locally:

```bash
# Run the full E2E test suite, which generates projects in tmp dirs
pytest tests/backend/test_project_bundle_import_e2e.py -v

# Or run the release gate tests
pytest -m release_gate -v
```

## See also

- [Release gate](../../docs/release/v1_acceptance_matrix.md)
- [Bundle import E2E](../../tests/backend/test_project_bundle_import_e2e.py)
