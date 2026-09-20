"""Repository implementations for v2 persistence."""

from manga_autopilot.repositories.work_lifecycle import (
    PortableWorkInspection,
    WorkCatalogEntry,
    WorkCreationError,
    WorkHandle,
    WorkIdentityMismatchError,
    WorkLifecycleRepository,
    WorkManifestError,
    WorkNotFoundError,
    WorkRecoveryError,
    WorkRecoveryFinding,
    WorkUpgradeError,
    inspect_work_directory,
)

__all__ = [
    "PortableWorkInspection",
    "WorkCatalogEntry",
    "WorkCreationError",
    "WorkHandle",
    "WorkIdentityMismatchError",
    "WorkLifecycleRepository",
    "WorkManifestError",
    "WorkNotFoundError",
    "WorkRecoveryError",
    "WorkRecoveryFinding",
    "WorkUpgradeError",
    "inspect_work_directory",
]
