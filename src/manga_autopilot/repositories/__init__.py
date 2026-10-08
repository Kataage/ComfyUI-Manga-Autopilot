"""Repository implementations for v2 persistence."""

from manga_autopilot.repositories.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRepository,
)
from manga_autopilot.repositories.page_domain import (
    LayoutRepository,
    PageDomainNotFoundError,
    PageDomainOwnershipError,
    PageRepository,
    PanelRepository,
)
from manga_autopilot.repositories.revision_invalidation import PageDomainAuditRepository
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
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactRepository",
    "PageDomainAuditRepository",
    "LayoutRepository",
    "PageDomainNotFoundError",
    "PageDomainOwnershipError",
    "PageRepository",
    "PanelRepository",
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
