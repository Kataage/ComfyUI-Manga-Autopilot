"""Legacy migration and import-discovery helpers."""

from manga_autopilot.migration.legacy_inventory import (
    LegacyInventoryEntry,
    LegacyInventoryWarning,
    LegacyProjectInventory,
    LegacyProjectInventoryError,
    LegacyProjectInventoryService,
    LegacyProjectNotFoundError,
)

__all__ = [
    "LegacyInventoryEntry",
    "LegacyInventoryWarning",
    "LegacyProjectInventory",
    "LegacyProjectInventoryError",
    "LegacyProjectInventoryService",
    "LegacyProjectNotFoundError",
]
