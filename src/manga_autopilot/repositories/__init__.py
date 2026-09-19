"""Repository implementations for v2 persistence."""

from manga_autopilot.repositories.work_lifecycle import (
    WorkCatalogEntry,
    WorkCreationError,
    WorkHandle,
    WorkIdentityMismatchError,
    WorkLifecycleRepository,
    WorkManifestError,
    WorkNotFoundError,
)

__all__ = [
    "WorkCatalogEntry",
    "WorkCreationError",
    "WorkHandle",
    "WorkIdentityMismatchError",
    "WorkLifecycleRepository",
    "WorkManifestError",
    "WorkNotFoundError",
]
