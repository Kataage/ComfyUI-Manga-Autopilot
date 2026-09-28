"""Tests for the canonical live/package Work manifest contract."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from manga_autopilot.storage import (
    PACKAGE_MANIFEST_HASH_POLICY,
    PACKAGE_MANIFEST_INTEGRITY_MODE,
    WORK_MANIFEST_FORMAT_VERSION,
    WorkManifestContractError,
    build_live_work_manifest,
    build_package_work_manifest,
    is_canonical_live_manifest,
    validate_manifest_common,
    verify_package_database,
)


def test_live_manifest_has_mutable_integrity_semantics() -> None:
    manifest = build_live_work_manifest(
        work_id="work_001",
        database_name="work.sqlite3",
        work_schema_version=3,
        created_at="2026-09-21T00:00:00+00:00",
        app_version="2.0-test",
    )

    assert manifest["format_version"] == WORK_MANIFEST_FORMAT_VERSION
    assert manifest["integrity"] == {
        "mode": "live_mutable",
        "database_hash_policy": "package_only",
    }
    assert "database_sha256" not in manifest["integrity"]
    assert is_canonical_live_manifest(manifest) is True


def test_package_manifest_hashes_exact_database_bytes(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    database.write_bytes(b"exact packaged sqlite bytes")

    manifest = build_package_work_manifest(
        work_id="work_001",
        database_path=database,
        work_schema_version=3,
        created_at="2026-09-21T00:00:00+00:00",
        app_version="2.0-test",
    )

    expected = hashlib.sha256(database.read_bytes()).hexdigest()
    assert manifest["format_version"] == WORK_MANIFEST_FORMAT_VERSION
    assert manifest["integrity"] == {
        "mode": PACKAGE_MANIFEST_INTEGRITY_MODE,
        "database_hash_policy": PACKAGE_MANIFEST_HASH_POLICY,
        "database_sha256": expected,
    }

    verify_package_database(manifest, database)


def test_package_verification_detects_changed_database_bytes(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    database.write_bytes(b"original")

    manifest = build_package_work_manifest(
        work_id="work_001",
        database_path=database,
        work_schema_version=3,
        created_at="2026-09-21T00:00:00+00:00",
        app_version="2.0-test",
    )

    database.write_bytes(b"changed")

    with pytest.raises(WorkManifestContractError, match="SHA-256 mismatch"):
        verify_package_database(manifest, database)


def test_package_builder_rejects_symlinked_database(tmp_path: Path) -> None:
    database = tmp_path / "outside.sqlite3"
    database.write_bytes(b"outside")
    link = tmp_path / "work.sqlite3"
    try:
        link.symlink_to(database)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(WorkManifestContractError, match="symlink"):
        build_package_work_manifest(
            work_id="work_001",
            database_path=link,
            work_schema_version=3,
            created_at="2026-09-21T00:00:00+00:00",
            app_version="2.0-test",
        )


def test_legacy_v1_manifest_is_accepted_only_as_compatibility_shape() -> None:
    manifest = {
        "format": "manga-autopilot-work",
        "format_version": 1,
        "work_id": "work_001",
        "database": "work.sqlite3",
        "work_schema_version": 1,
        "created_at": "2026-09-21T00:00:00+00:00",
        "app_version": "legacy",
        "integrity": {"database_sha256": "0" * 64},
    }

    version = validate_manifest_common(manifest, expected_work_id="work_001")

    assert version == 1
    assert is_canonical_live_manifest(manifest) is False


@pytest.mark.parametrize(
    "format_version",
    [True, False, 1.0, 2.0],
)
def test_manifest_format_version_requires_builtin_integer(
    format_version: object,
) -> None:
    manifest = build_live_work_manifest(
        work_id="work_001",
        database_name="work.sqlite3",
        work_schema_version=1,
        created_at="2026-09-21T00:00:00+00:00",
        app_version="test",
    )
    manifest["format_version"] = format_version

    with pytest.raises(
        WorkManifestContractError,
        match="format_version must be an integer",
    ):
        validate_manifest_common(manifest, expected_work_id="work_001")

    assert is_canonical_live_manifest(manifest) is False


@pytest.mark.parametrize(
    "schema_version",
    [True, False, 1.0, 2.0],
)
def test_manifest_schema_version_requires_builtin_positive_integer(
    schema_version: object,
) -> None:
    manifest = build_live_work_manifest(
        work_id="work_001",
        database_name="work.sqlite3",
        work_schema_version=1,
        created_at="2026-09-21T00:00:00+00:00",
        app_version="test",
    )
    manifest["work_schema_version"] = schema_version

    with pytest.raises(
        WorkManifestContractError,
        match="work_schema_version must be a positive integer",
    ):
        validate_manifest_common(manifest, expected_work_id="work_001")

    assert is_canonical_live_manifest(manifest) is False


@pytest.mark.parametrize("schema_version", [True, False, 1.0, 2.0])
def test_manifest_builders_reject_non_builtin_integer_schema_versions(
    tmp_path: Path,
    schema_version: object,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        build_live_work_manifest(
            work_id="work_001",
            database_name="work.sqlite3",
            work_schema_version=schema_version,  # type: ignore[arg-type]
            created_at="2026-09-21T00:00:00+00:00",
            app_version="test",
        )

    database = tmp_path / "work.sqlite3"
    database.write_bytes(b"package")
    with pytest.raises(ValueError, match="positive integer"):
        build_package_work_manifest(
            work_id="work_001",
            database_path=database,
            work_schema_version=schema_version,  # type: ignore[arg-type]
            created_at="2026-09-21T00:00:00+00:00",
            app_version="test",
        )


def test_future_manifest_format_is_rejected() -> None:
    manifest = build_live_work_manifest(
        work_id="work_001",
        database_name="work.sqlite3",
        work_schema_version=1,
        created_at="2026-09-21T00:00:00+00:00",
        app_version="future",
    )
    manifest["format_version"] = WORK_MANIFEST_FORMAT_VERSION + 1

    with pytest.raises(WorkManifestContractError, match="unsupported"):
        validate_manifest_common(manifest, expected_work_id="work_001")
