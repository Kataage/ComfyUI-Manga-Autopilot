"""Canonical Work manifest contract.

Live Work manifests describe mutable work directories and therefore do not hash
`work.sqlite3`. Immutable package manifests hash the exact packaged database
bytes. The Work DB remains the semantic Source of Truth in both cases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manga_autopilot.primitives import sha256_file

WORK_MANIFEST_FORMAT = "manga-autopilot-work"
WORK_MANIFEST_FORMAT_VERSION = 2
LEGACY_WORK_MANIFEST_FORMAT_VERSIONS = frozenset({1})

LIVE_MANIFEST_INTEGRITY_MODE = "live_mutable"
LIVE_MANIFEST_HASH_POLICY = "package_only"

PACKAGE_MANIFEST_INTEGRITY_MODE = "immutable_package"
PACKAGE_MANIFEST_HASH_POLICY = "sha256"


class WorkManifestContractError(ValueError):
    """Raised when manifest data violates the canonical contract."""


def build_live_work_manifest(
    *,
    work_id: str,
    database_name: str,
    work_schema_version: int,
    created_at: str,
    app_version: str | None,
) -> dict[str, Any]:
    """Build canonical format-v2 metadata for one mutable live Work."""
    _validate_common_inputs(
        work_id=work_id,
        database_name=database_name,
        work_schema_version=work_schema_version,
        created_at=created_at,
    )
    return {
        "format": WORK_MANIFEST_FORMAT,
        "format_version": WORK_MANIFEST_FORMAT_VERSION,
        "work_id": work_id,
        "database": database_name,
        "work_schema_version": work_schema_version,
        "created_at": created_at,
        "app_version": app_version,
        "integrity": {
            "mode": LIVE_MANIFEST_INTEGRITY_MODE,
            "database_hash_policy": LIVE_MANIFEST_HASH_POLICY,
        },
    }


def build_package_work_manifest(
    *,
    work_id: str,
    database_path: str | Path,
    work_schema_version: int,
    created_at: str,
    app_version: str | None,
) -> dict[str, Any]:
    """Build an immutable package manifest for exact packaged DB bytes."""
    path = Path(database_path)
    if path.is_symlink():
        raise WorkManifestContractError(
            f"package database must not be a symlink: {path}"
        )
    if not path.is_file():
        raise WorkManifestContractError(
            f"package database must be a regular file: {path}"
        )
    _validate_common_inputs(
        work_id=work_id,
        database_name=path.name,
        work_schema_version=work_schema_version,
        created_at=created_at,
    )
    return {
        "format": WORK_MANIFEST_FORMAT,
        "format_version": WORK_MANIFEST_FORMAT_VERSION,
        "work_id": work_id,
        "database": path.name,
        "work_schema_version": work_schema_version,
        "created_at": created_at,
        "app_version": app_version,
        "integrity": {
            "mode": PACKAGE_MANIFEST_INTEGRITY_MODE,
            "database_hash_policy": PACKAGE_MANIFEST_HASH_POLICY,
            "database_sha256": sha256_file(path),
        },
    }


def validate_manifest_common(
    manifest: dict[str, Any],
    *,
    expected_work_id: str | None = None,
    expected_database_name: str = "work.sqlite3",
) -> int:
    """Validate shared identity/schema fields and return format version."""
    if manifest.get("format") != WORK_MANIFEST_FORMAT:
        raise WorkManifestContractError(
            f"unsupported Work manifest format: {manifest.get('format')!r}"
        )

    format_version = manifest.get("format_version")
    accepted_versions = {
        WORK_MANIFEST_FORMAT_VERSION,
        *LEGACY_WORK_MANIFEST_FORMAT_VERSIONS,
    }
    if format_version not in accepted_versions:
        raise WorkManifestContractError(
            f"unsupported Work manifest format_version: {format_version!r}"
        )

    work_id = manifest.get("work_id")
    if not isinstance(work_id, str) or not work_id:
        raise WorkManifestContractError(
            "Work manifest work_id must be a non-empty string"
        )
    if expected_work_id is not None and work_id != expected_work_id:
        raise WorkManifestContractError(
            f"manifest Work identity mismatch: expected {expected_work_id!r}, "
            f"got {work_id!r}"
        )

    if manifest.get("database") != expected_database_name:
        raise WorkManifestContractError(
            f"manifest database mismatch: expected {expected_database_name!r}, "
            f"got {manifest.get('database')!r}"
        )

    schema_version = manifest.get("work_schema_version")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise WorkManifestContractError(
            "Work manifest work_schema_version must be a positive integer"
        )

    return int(format_version)


def is_canonical_live_manifest(manifest: dict[str, Any]) -> bool:
    """Return whether a manifest uses canonical format-v2 live semantics."""
    integrity = manifest.get("integrity")
    return (
        manifest.get("format") == WORK_MANIFEST_FORMAT
        and manifest.get("format_version") == WORK_MANIFEST_FORMAT_VERSION
        and isinstance(integrity, dict)
        and integrity.get("mode") == LIVE_MANIFEST_INTEGRITY_MODE
        and integrity.get("database_hash_policy") == LIVE_MANIFEST_HASH_POLICY
        and "database_sha256" not in integrity
    )


def verify_package_database(
    manifest: dict[str, Any],
    database_path: str | Path,
) -> None:
    """Verify exact DB bytes against a canonical immutable package manifest."""
    path = Path(database_path)
    validate_manifest_common(
        manifest,
        expected_work_id=str(manifest.get("work_id") or ""),
        expected_database_name=path.name,
    )
    if manifest.get("format_version") != WORK_MANIFEST_FORMAT_VERSION:
        raise WorkManifestContractError(
            "package verification requires canonical format_version 2"
        )

    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        raise WorkManifestContractError("package manifest integrity block is required")
    if integrity.get("mode") != PACKAGE_MANIFEST_INTEGRITY_MODE:
        raise WorkManifestContractError(
            "package manifest integrity.mode must be 'immutable_package'"
        )
    if integrity.get("database_hash_policy") != PACKAGE_MANIFEST_HASH_POLICY:
        raise WorkManifestContractError(
            "package manifest database_hash_policy must be 'sha256'"
        )

    expected = integrity.get("database_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise WorkManifestContractError(
            "package manifest database_sha256 must be a SHA-256 hex digest"
        )
    if path.is_symlink() or not path.is_file():
        raise WorkManifestContractError(
            f"package database must be a regular non-symlink file: {path}"
        )
    actual = sha256_file(path)
    if actual != expected:
        raise WorkManifestContractError(
            f"package database SHA-256 mismatch: expected {expected}, got {actual}"
        )


def _validate_common_inputs(
    *,
    work_id: str,
    database_name: str,
    work_schema_version: int,
    created_at: str,
) -> None:
    if not work_id:
        raise ValueError("work_id must be non-empty")
    if not database_name or "/" in database_name or "\\" in database_name:
        raise ValueError("database_name must be one filename")
    if work_schema_version < 1:
        raise ValueError("work_schema_version must be positive")
    if not created_at:
        raise ValueError("created_at must be non-empty")


__all__ = [
    "LEGACY_WORK_MANIFEST_FORMAT_VERSIONS",
    "LIVE_MANIFEST_HASH_POLICY",
    "LIVE_MANIFEST_INTEGRITY_MODE",
    "PACKAGE_MANIFEST_HASH_POLICY",
    "PACKAGE_MANIFEST_INTEGRITY_MODE",
    "WORK_MANIFEST_FORMAT",
    "WORK_MANIFEST_FORMAT_VERSION",
    "WorkManifestContractError",
    "build_live_work_manifest",
    "build_package_work_manifest",
    "is_canonical_live_manifest",
    "validate_manifest_common",
    "verify_package_database",
]
