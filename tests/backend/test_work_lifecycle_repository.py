"""Integration tests for the v2 Work lifecycle repository."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

import manga_autopilot.repositories.work_lifecycle as lifecycle_module
from manga_autopilot.primitives import canonical_json
from manga_autopilot.repositories import (
    WorkCreationError,
    WorkIdentityMismatchError,
    WorkLifecycleRepository,
    WorkRecoveryError,
    WorkUpgradeError,
    inspect_work_directory,
)
from manga_autopilot.storage import (
    WORK_MIGRATIONS,
    Migration,
    UnsafeStoragePathError,
    bootstrap_master_database,
    repository_read,
    repository_write,
)


def _future_work_migration(*, broken: bool = False) -> tuple[Migration, ...]:
    version = WORK_MIGRATIONS[-1].version + 1
    statements = (
        ("INSERT INTO missing_upgrade_table (id) VALUES (1)",)
        if broken
        else ("CREATE TABLE upgrade_probe (id INTEGER PRIMARY KEY)",)
    )
    return (
        *WORK_MIGRATIONS,
        Migration(
            version=version,
            name=f"W{version:04d}_test_upgrade",
            statements=statements,
        ),
    )


def _recovery_tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _hold_work_creation_lock_in_child(
    storage_root: str,
    work_id: str,
    ready: object,
    release: object,
) -> None:
    works_root = Path(storage_root) / "works"
    staging = works_root / f".creating-{work_id}"
    with lifecycle_module._work_creation_lock(works_root, work_id):
        staging.mkdir(exist_ok=False)
        (staging / "partial.txt").write_text("owned", encoding="utf-8")
        ready.set()
        if not release.wait(20):
            raise RuntimeError("timed out waiting to release child owner")


def _assert_initial_work_revision(database: Path, work_id: str) -> None:
    with repository_read(database) as connection:
        commits = connection.execute(
            "SELECT * FROM commits ORDER BY commit_seq"
        ).fetchall()
        metadata = connection.execute(
            "SELECT * FROM work_metadata WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        revisions = connection.execute(
            """
            SELECT *
            FROM entity_revisions
            WHERE entity_type = 'work' AND entity_id = ?
            ORDER BY entity_revision
            """,
            (work_id,),
        ).fetchall()

    assert len(commits) == 1
    assert metadata is not None
    assert len(revisions) == 1

    commit = commits[0]
    revision = revisions[0]
    assert commit["actor_type"] == "system"
    assert commit["operation_type"] == "create_work"
    assert metadata["current_commit_seq"] == commit["commit_seq"]
    assert metadata["current_revision"] == 1

    assert revision["entity_type"] == "work"
    assert revision["entity_id"] == work_id
    assert revision["entity_revision"] == 1
    assert revision["commit_seq"] == commit["commit_seq"]
    assert revision["change_kind"] == "create"
    assert revision["before_json"] is None
    assert revision["created_at"] == commit["created_at"]

    expected_state = {
        "schema_version": 1,
        "work_id": work_id,
        "universe_source_id": metadata["universe_source_id"],
        "series_source_id": metadata["series_source_id"],
        "source_checkpoint_id": metadata["source_checkpoint_id"],
        "title": metadata["title"],
        "work_kind": metadata["work_kind"],
        "language": metadata["language"],
        "reading_direction": metadata["reading_direction"],
        "status": metadata["status"],
        "current_revision": metadata["current_revision"],
        "created_at": metadata["created_at"],
        "updated_at": metadata["updated_at"],
        "completed_at": metadata["completed_at"],
    }
    assert revision["after_json"] == canonical_json(expected_state)


def test_concurrent_same_work_creator_cannot_delete_owner_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    owner_errors: list[BaseException] = []
    owner_handles: list[object] = []
    original_bootstrap = lifecycle_module.bootstrap_work_database

    def blocking_bootstrap(*args: object, **kwargs: object):
        if kwargs.get("work_id") == "work_race":
            entered.set()
            if not release.wait(20):
                raise RuntimeError("timed out waiting to continue owner creation")
        return original_bootstrap(*args, **kwargs)

    monkeypatch.setattr(
        lifecycle_module,
        "bootstrap_work_database",
        blocking_bootstrap,
    )

    def owner_create() -> None:
        try:
            owner_handles.append(
                repository.create_work(
                    work_id="work_race",
                    title="Winning Creator",
                )
            )
        except BaseException as exc:
            owner_errors.append(exc)

    owner = threading.Thread(target=owner_create)
    owner.start()
    assert entered.wait(20)

    staging = tmp_path / "works" / ".creating-work_race"
    evidence = staging / "owner-evidence.txt"
    evidence.write_text("do not delete", encoding="utf-8")

    try:
        with pytest.raises(WorkCreationError, match="already active|already exists"):
            repository.create_work(
                work_id="work_race",
                title="Losing Creator",
            )
        assert evidence.read_text(encoding="utf-8") == "do not delete"
    finally:
        release.set()
        owner.join(20)

    assert not owner.is_alive()
    assert owner_errors == []
    assert len(owner_handles) == 1
    final_evidence = tmp_path / "works" / "work_race" / evidence.name
    assert final_evidence.read_text(encoding="utf-8") == "do not delete"
    assert [entry.work_id for entry in repository.list_works()] == ["work_race"]


def test_active_cross_process_staging_is_not_recoverable(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    owner = context.Process(
        target=_hold_work_creation_lock_in_child,
        args=(str(tmp_path), "work_active", ready, release),
    )
    owner.start()
    assert ready.wait(20)

    staging = tmp_path / "works" / ".creating-work_active"
    try:
        findings = repository.scan_recovery()
        assert len(findings) == 1
        finding = findings[0]
        assert finding.kind == "ACTIVE_STAGING"
        assert finding.work_id == "work_active"
        assert finding.valid is False
        assert finding.recommended_action is None
        assert "active creator" in " ".join(finding.diagnostics)

        with pytest.raises(WorkRecoveryError, match="still active"):
            repository.finalize_staging_work("work_active")
        with pytest.raises(WorkRecoveryError, match="still active"):
            repository.quarantine_staging_work("work_active")
        assert (staging / "partial.txt").read_text(encoding="utf-8") == "owned"
    finally:
        release.set()
        owner.join(20)

    assert owner.exitcode == 0
    findings = repository.scan_recovery()
    assert len(findings) == 1
    assert findings[0].kind == "STALE_STAGING_INVALID"
    assert findings[0].recommended_action == "quarantine"

    destination = repository.quarantine_staging_work(
        "work_active",
        reason="owner exited",
    )
    assert (destination / "partial.txt").read_text(encoding="utf-8") == "owned"
    assert repository.scan_recovery() == ()


def test_create_does_not_clean_up_preexisting_unowned_staging(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    staging = tmp_path / "works" / ".creating-work_existing"
    staging.mkdir()
    evidence = staging / "partial.txt"
    evidence.write_text("preexisting evidence", encoding="utf-8")

    with pytest.raises(WorkCreationError, match="staging directory already exists"):
        repository.create_work(
            work_id="work_existing",
            title="Must Not Delete",
        )

    assert staging.is_dir()
    assert evidence.read_text(encoding="utf-8") == "preexisting evidence"


def test_create_open_list_and_reopen_work(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path, app_version="test")

    created = repository.create_work(
        work_id="work_001",
        title="First Manga",
        work_kind="standalone",
        language="ja",
        reading_direction="RTL_TOP_TO_BOTTOM",
    )

    assert created.work_id == "work_001"
    assert created.title == "First Manga"
    assert created.root == (tmp_path / "works" / "work_001").resolve()
    assert created.database_path.is_file()
    assert created.manifest_path.is_file()
    assert created.current_revision == 1
    assert created.schema_version == WORK_MIGRATIONS[-1].version
    assert created.migration_backup_path is None

    listed = repository.list_works()
    assert [entry.work_id for entry in listed] == ["work_001"]
    assert listed[0].relative_work_path == "works/work_001"
    assert listed[0].manifest_hash

    reopened_repository = WorkLifecycleRepository(tmp_path, app_version="test")
    reopened = reopened_repository.open_work("work_001")

    assert reopened.work_id == created.work_id
    assert reopened.title == created.title
    assert reopened.current_commit_seq == created.current_commit_seq
    assert reopened.catalog.last_opened_at is not None
    assert reopened.migration_backup_path is None
    _assert_initial_work_revision(created.database_path, created.work_id)


def test_initial_work_revision_survives_reopen_without_duplication(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(
        work_id="work_revision",
        title="Revision History",
    )
    _assert_initial_work_revision(created.database_path, created.work_id)

    reopened = WorkLifecycleRepository(tmp_path).open_work(created.work_id)

    assert reopened.current_revision == 1
    _assert_initial_work_revision(reopened.database_path, reopened.work_id)


def test_initial_work_transaction_rolls_back_when_revision_recording_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)

    def fail_revision(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated revision failure")

    monkeypatch.setattr(lifecycle_module, "create_work_entity_revision", fail_revision)
    monkeypatch.setattr(
        lifecycle_module.shutil,
        "rmtree",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(WorkCreationError, match="simulated revision failure"):
        repository.create_work(
            work_id="work_atomic",
            title="Atomic Creation",
        )

    staging = tmp_path / "works" / ".creating-work_atomic"
    database = staging / "work.sqlite3"
    assert database.is_file()

    with repository_read(database) as connection:
        commit_count = connection.execute(
            "SELECT COUNT(*) FROM commits"
        ).fetchone()[0]
        metadata_count = connection.execute(
            "SELECT COUNT(*) FROM work_metadata"
        ).fetchone()[0]
        revision_count = connection.execute(
            "SELECT COUNT(*) FROM entity_revisions"
        ).fetchone()[0]

    assert commit_count == 0
    assert metadata_count == 0
    assert revision_count == 0

    with repository_read(repository.paths.master_db) as connection:
        catalog = connection.execute(
            "SELECT 1 FROM work_catalog WHERE work_id = 'work_atomic'"
        ).fetchone()
    assert catalog is None


@pytest.mark.parametrize(
    "lineage_kwargs",
    [
        {"universe_id": "universe_001"},
        {"series_id": "series_001"},
        {"universe_id": "universe_001", "series_id": "series_001"},
    ],
)
def test_create_work_rejects_master_lineage_without_source_snapshots(
    tmp_path: Path,
    lineage_kwargs: dict[str, str],
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    works_before = tuple(repository.paths.works.iterdir())

    with pytest.raises(ValueError, match="immutable source snapshots"):
        repository.create_work(
            work_id="work_linked",
            title="Must Be Snapshotted",
            **lineage_kwargs,
        )

    assert tuple(repository.paths.works.iterdir()) == works_before
    assert repository.list_works() == ()


def test_standalone_work_persists_no_master_lineage_and_reopens(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(
        work_id="work_standalone",
        title="Standalone",
    )

    with repository_read(created.database_path) as connection:
        metadata = connection.execute(
            """
            SELECT universe_source_id, series_source_id, source_checkpoint_id
            FROM work_metadata
            WHERE work_id = 'work_standalone'
            """
        ).fetchone()
    assert metadata is not None
    assert tuple(metadata) == (None, None, None)

    listed = repository.list_works()
    assert len(listed) == 1
    assert listed[0].universe_id is None
    assert listed[0].series_id is None

    reopened = WorkLifecycleRepository(tmp_path).open_work("work_standalone")
    assert reopened.work_id == "work_standalone"


@pytest.mark.parametrize(
    ("storage_location", "column"),
    [
        ("catalog", "universe_id"),
        ("catalog", "series_id"),
        ("work", "universe_source_id"),
        ("work", "series_source_id"),
        ("work", "source_checkpoint_id"),
    ],
)
def test_open_rejects_unsnapshotted_persisted_master_lineage(
    tmp_path: Path,
    storage_location: str,
    column: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(
        work_id="work_legacy_linked",
        title="Legacy Linked",
    )

    if storage_location == "catalog":
        with repository_write(repository.paths.master_db) as connection:
            connection.execute(
                f"UPDATE work_catalog SET {column} = ? WHERE work_id = ?",
                ("source_001", created.work_id),
            )
    else:
        with repository_write(created.database_path) as connection:
            connection.execute(
                f"UPDATE work_metadata SET {column} = ? WHERE work_id = ?",
                ("source_001", created.work_id),
            )

    with pytest.raises(
        WorkIdentityMismatchError,
        match="immutable source snapshots",
    ):
        repository.open_work(created.work_id)


def test_open_rejects_unsnapshotted_lineage_before_upgrade_mutation(
    tmp_path: Path,
) -> None:
    base_repository = WorkLifecycleRepository(tmp_path)
    created = base_repository.create_work(
        work_id="work_linked_before_upgrade",
        title="Linked Before Upgrade",
    )
    with repository_write(created.database_path) as connection:
        connection.execute(
            """
            UPDATE work_metadata
            SET series_source_id = 'series_legacy'
            WHERE work_id = ?
            """,
            (created.work_id,),
        )

    future_migrations = _future_work_migration()
    target_version = future_migrations[-1].version
    repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=future_migrations,
    )

    with pytest.raises(
        WorkIdentityMismatchError,
        match="immutable source snapshots",
    ):
        repository.open_work(created.work_id)

    with repository_read(created.database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'upgrade_probe'"
        ).fetchone() is None
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    assert version == WORK_MIGRATIONS[-1].version
    backup = created.database_path.with_name(
        f"{created.database_path.name}.backup-v{WORK_MIGRATIONS[-1].version}"
        f"-to-v{target_version}"
    )
    assert not backup.exists()


@pytest.mark.parametrize(
    "reserved_id",
    [
        ".creating-work_001",
        ".recovery-quarantine",
        ".future-internal",
    ],
)
def test_create_work_rejects_reserved_id_before_work_filesystem_mutation(
    tmp_path: Path,
    reserved_id: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    before = tuple(repository.paths.works.iterdir())

    with pytest.raises(ValueError, match="reserved"):
        repository.create_work(work_id=reserved_id, title="Must Not Create")

    assert tuple(repository.paths.works.iterdir()) == before


@pytest.mark.parametrize(
    "operation",
    [
        "reconcile_orphan_work",
        "finalize_staging_work",
        "quarantine_staging_work",
    ],
)
def test_recovery_operations_reject_reserved_work_ids(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    before = tuple(repository.paths.works.iterdir())

    with pytest.raises(ValueError, match="reserved"):
        getattr(repository, operation)(".creating-not-a-work")

    assert tuple(repository.paths.works.iterdir()) == before


def test_portable_inspection_rejects_reserved_manifest_work_id(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path / "library")
    created = repository.create_work(work_id="work_001", title="Portable")

    portable_root = tmp_path / "portable"
    shutil.copytree(created.root, portable_root)
    manifest_path = portable_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["work_id"] = ".creating-not-a-work"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(lifecycle_module.WorkManifestError, match="reserved"):
        inspect_work_directory(portable_root)


def test_create_work_writes_live_mutable_manifest(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path, app_version="2.0-test")
    handle = repository.create_work(work_id="work_001", title="Manifest Test")

    manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))

    assert manifest["format"] == "manga-autopilot-work"
    assert manifest["format_version"] == 2
    assert manifest["work_id"] == "work_001"
    assert manifest["database"] == "work.sqlite3"
    assert manifest["work_schema_version"] == WORK_MIGRATIONS[-1].version
    assert manifest["created_at"]
    assert manifest["app_version"] == "2.0-test"
    assert manifest["integrity"] == {
        "mode": "live_mutable",
        "database_hash_policy": "package_only",
    }
    assert "database_sha256" not in manifest["integrity"]


def test_legitimate_work_db_edit_does_not_trigger_false_hash_corruption(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Catalog Title")

    with repository_write(created.database_path) as connection:
        connection.execute(
            """
            UPDATE work_metadata
            SET title = 'Authoritative DB Title',
                current_revision = current_revision + 1,
                updated_at = '2026-09-20T01:00:00+00:00'
            WHERE work_id = 'work_001'
            """
        )

    opened = repository.open_work("work_001")
    listed = repository.list_works()

    assert opened.title == "Authoritative DB Title"
    assert opened.current_revision == 2
    assert listed[0].title == "Catalog Title"


def test_open_does_not_read_legacy_project_json(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    repository.create_work(work_id="work_001", title="V2 Work")

    legacy_dir = tmp_path / "projects" / "work_001"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "project.json").write_text(
        "{ definitely not valid json",
        encoding="utf-8",
    )

    opened = repository.open_work("work_001")

    assert opened.title == "V2 Work"


def test_open_detects_manifest_work_identity_mismatch(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    handle = repository.create_work(work_id="work_001", title="Identity")

    manifest = json.loads(handle.manifest_path.read_text(encoding="utf-8"))
    manifest["work_id"] = "work_other"
    handle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(WorkIdentityMismatchError, match="manifest Work identity mismatch"):
        repository.open_work("work_001")


def test_open_detects_database_work_identity_mismatch(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    handle = repository.create_work(work_id="work_001", title="Identity")

    with repository_write(handle.database_path) as connection:
        connection.execute(
            """
            UPDATE work_database_metadata
            SET value = 'work_other'
            WHERE key = 'work_id'
            """
        )

    with pytest.raises(WorkIdentityMismatchError, match="Work DB identity mismatch"):
        repository.open_work("work_001")


def test_create_rejects_duplicate_work_id_without_overwrite(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    first = repository.create_work(work_id="work_001", title="First")

    with pytest.raises(WorkCreationError, match="already exists"):
        repository.create_work(work_id="work_001", title="Second")

    assert repository.open_work("work_001").title == "First"
    assert first.database_path.is_file()


def test_directory_fsync_is_noop_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_open(*args: object, **kwargs: object) -> int:
        raise AssertionError("directory open must not be attempted on Windows")

    monkeypatch.setattr(lifecycle_module.os, "name", "nt")
    monkeypatch.setattr(lifecycle_module.os, "open", unexpected_open)

    lifecycle_module._fsync_directory(tmp_path)


def test_atomic_json_write_fsyncs_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "manifest.json"
    fsynced: list[Path] = []

    monkeypatch.setattr(
        lifecycle_module,
        "_fsync_directory",
        lambda directory: fsynced.append(Path(directory)),
    )

    lifecycle_module._write_json_atomic(path, {"work_id": "work_001"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "work_id": "work_001"
    }
    assert fsynced == [tmp_path]


def test_durable_directory_publish_fsyncs_before_and_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ".creating-work_001"
    destination = tmp_path / "work_001"
    source.mkdir()
    events: list[str] = []
    original_replace = os.replace

    def record_fsync(path: Path) -> None:
        events.append(f"fsync:{Path(path).name}")

    def record_replace(
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
    ) -> None:
        events.append("replace")
        original_replace(src, dst)

    monkeypatch.setattr(lifecycle_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(lifecycle_module.os, "replace", record_replace)

    lifecycle_module._durable_replace_directory(source, destination)

    assert destination.is_dir()
    assert events == [
        "fsync:.creating-work_001",
        "replace",
        f"fsync:{tmp_path.name}",
    ]


def test_create_catalog_registration_happens_after_durable_directory_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    events: list[str] = []
    original_publish = lifecycle_module._durable_replace_directory
    original_repository_write = lifecycle_module.repository_write

    def record_publish(source: Path, destination: Path) -> None:
        events.append("publish")
        original_publish(source, destination)

    def record_repository_write(database_path: str | Path):
        if Path(database_path).resolve() == repository.paths.master_db.resolve():
            events.append("master-write")
        return original_repository_write(database_path)

    monkeypatch.setattr(
        lifecycle_module,
        "_durable_replace_directory",
        record_publish,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "repository_write",
        record_repository_write,
    )

    repository.create_work(work_id="work_ordered", title="Ordered")

    assert "publish" in events
    assert "master-write" in events
    assert events.index("publish") < events.index("master-write")


def test_parent_directory_fsync_failure_leaves_recoverable_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    original_fsync = lifecycle_module._fsync_directory

    def fail_works_parent(path: Path) -> None:
        directory = Path(path)
        if directory.resolve() == repository.paths.works.resolve():
            raise OSError("simulated parent directory fsync failure")
        original_fsync(directory)

    monkeypatch.setattr(lifecycle_module, "_fsync_directory", fail_works_parent)

    with pytest.raises(
        WorkCreationError,
        match="simulated parent directory fsync failure",
    ):
        repository.create_work(
            work_id="work_fsync_orphan",
            title="Durability Failure",
        )

    final_root = tmp_path / "works" / "work_fsync_orphan"
    assert final_root.is_dir()
    assert not (tmp_path / "works" / ".creating-work_fsync_orphan").exists()
    assert repository.list_works() == ()

    findings = repository.scan_recovery()
    assert len(findings) == 1
    assert findings[0].kind == "UNREGISTERED_WORK_VALID"
    assert findings[0].work_id == "work_fsync_orphan"
    assert findings[0].recommended_action == "register"


def test_create_cleans_staging_on_filesystem_finalize_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    original_replace = os.replace

    def fail_directory_replace(
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
    ) -> None:
        source = Path(src)
        if source.name == ".creating-work_fail":
            raise OSError("simulated directory rename failure")
        original_replace(src, dst)

    monkeypatch.setattr(lifecycle_module.os, "replace", fail_directory_replace)

    with pytest.raises(WorkCreationError, match="simulated directory rename failure"):
        repository.create_work(work_id="work_fail", title="Will Fail")

    assert not (tmp_path / "works" / "work_fail").exists()
    assert not (tmp_path / "works" / ".creating-work_fail").exists()
    assert repository.list_works() == ()


def test_catalog_registration_failure_preserves_recoverable_orphan_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    original_repository_write = lifecycle_module.repository_write
    master_write_count = 0

    def selective_repository_write(database_path: str | Path):
        nonlocal master_write_count
        if Path(database_path).resolve() == repository.paths.master_db.resolve():
            master_write_count += 1
            if master_write_count == 1:
                raise RuntimeError("simulated catalog write failure")
        return original_repository_write(database_path)

    monkeypatch.setattr(
        lifecycle_module,
        "repository_write",
        selective_repository_write,
    )

    with pytest.raises(WorkCreationError, match="simulated catalog write failure"):
        repository.create_work(work_id="work_fail", title="Will Fail")

    final_root = tmp_path / "works" / "work_fail"
    assert final_root.is_dir()
    assert (final_root / "work.sqlite3").is_file()
    assert (final_root / "manifest.json").is_file()
    assert not (tmp_path / "works" / ".creating-work_fail").exists()

    with repository_read(repository.paths.master_db) as connection:
        row = connection.execute(
            "SELECT 1 FROM work_catalog WHERE work_id = 'work_fail'"
        ).fetchone()
    assert row is None

    findings = repository.scan_recovery()
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "UNREGISTERED_WORK_VALID"
    assert finding.work_id == "work_fail"
    assert finding.valid is True
    assert finding.recommended_action == "register"
    assert finding.diagnostics == ()

    first = repository.reconcile_orphan_work("work_fail")
    second = repository.reconcile_orphan_work("work_fail")

    assert first.work_id == "work_fail"
    assert second == first
    opened = repository.open_work("work_fail")
    assert opened.title == "Will Fail"
    _assert_initial_work_revision(opened.database_path, opened.work_id)
    assert repository.scan_recovery() == ()


def test_recovery_scan_reports_catalog_work_with_missing_root(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(
        work_id="work_missing_root",
        title="Missing Root",
    )

    shutil.rmtree(created.root)

    findings = repository.scan_recovery()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "CATALOG_WORK_MISSING"
    assert finding.work_id == created.work_id
    assert finding.path == created.root
    assert finding.valid is False
    assert finding.recommended_action is None
    assert "missing Work directory" in " ".join(finding.diagnostics)
    assert [entry.work_id for entry in repository.list_works()] == [created.work_id]


@pytest.mark.parametrize("critical_name", ["work.sqlite3", "manifest.json"])
def test_recovery_scan_reports_catalog_work_with_missing_critical_file(
    tmp_path: Path,
    critical_name: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(
        work_id=f"work_missing_{critical_name.split('.')[0]}",
        title="Missing Critical File",
    )
    critical_path = created.root / critical_name
    critical_path.unlink()

    findings = repository.scan_recovery()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "CATALOG_WORK_INCOMPLETE"
    assert finding.work_id == created.work_id
    assert finding.path == created.root
    assert finding.valid is False
    assert finding.recommended_action is None
    assert str(critical_path) in " ".join(finding.diagnostics)
    assert [entry.work_id for entry in repository.list_works()] == [created.work_id]


def test_recovery_scan_reports_unsafe_catalog_path_without_repair(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(
        work_id="work_unsafe_catalog",
        title="Unsafe Catalog",
    )
    with repository_write(repository.paths.master_db) as connection:
        connection.execute(
            """
            UPDATE work_catalog
            SET relative_work_path = '../outside'
            WHERE work_id = ?
            """,
            (created.work_id,),
        )

    findings = repository.scan_recovery()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "CATALOG_WORK_PATH_INVALID"
    assert finding.work_id == created.work_id
    assert finding.valid is False
    assert finding.recommended_action is None
    assert "unsafe catalog work path" in " ".join(finding.diagnostics)
    assert [entry.work_id for entry in repository.list_works()] == [created.work_id]


def test_catalog_path_escape_is_rejected(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    repository.create_work(work_id="work_001", title="Safe")

    with repository_write(repository.paths.master_db) as connection:
        connection.execute(
            """
            UPDATE work_catalog
            SET relative_work_path = '../outside'
            WHERE work_id = 'work_001'
            """
        )

    with pytest.raises(WorkIdentityMismatchError, match="unsafe catalog work path"):
        repository.open_work("work_001")


def test_generated_work_id_is_opaque_and_cataloged(tmp_path: Path) -> None:
    repository = WorkLifecycleRepository(tmp_path)

    created = repository.create_work(title="Generated ID")

    assert created.work_id.startswith("work_")
    assert "/" not in created.work_id
    assert repository.list_works()[0].work_id == created.work_id


def test_open_upgrades_work_from_schema_n_to_n_plus_1(tmp_path: Path) -> None:
    old_repository = WorkLifecycleRepository(
        tmp_path,
        app_version="old",
        work_migrations=WORK_MIGRATIONS,
    )
    created = old_repository.create_work(work_id="work_001", title="Upgrade Me")
    old_version = created.schema_version
    future_migrations = _future_work_migration()
    new_version = future_migrations[-1].version

    inspection_before = inspect_work_directory(
        created.root,
        migrations=future_migrations,
    )
    assert inspection_before.database_schema_version == old_version
    assert inspection_before.upgrade_required is True

    new_repository = WorkLifecycleRepository(
        tmp_path,
        app_version="new",
        work_migrations=future_migrations,
    )
    opened = new_repository.open_work("work_001")

    assert opened.schema_version == new_version
    assert opened.migration_backup_path is not None
    assert opened.migration_backup_path.is_file()

    with repository_read(opened.database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'upgrade_probe'"
        ).fetchone() is not None
        actual_version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    manifest = json.loads(opened.manifest_path.read_text(encoding="utf-8"))
    assert actual_version == new_version
    assert manifest["work_schema_version"] == new_version
    assert manifest["format_version"] == 2
    assert manifest["integrity"]["mode"] == "live_mutable"


def test_reopening_already_upgraded_work_is_idempotent(tmp_path: Path) -> None:
    base_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=WORK_MIGRATIONS,
    )
    base_repository.create_work(work_id="work_001", title="Upgrade Once")
    future_migrations = _future_work_migration()

    upgraded_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=future_migrations,
    )
    first = upgraded_repository.open_work("work_001")
    second = upgraded_repository.open_work("work_001")

    assert first.schema_version == future_migrations[-1].version
    assert first.migration_backup_path is not None
    assert second.schema_version == first.schema_version
    assert second.migration_backup_path is None


def test_failed_work_upgrade_does_not_expose_partially_upgraded_work(
    tmp_path: Path,
) -> None:
    base_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=WORK_MIGRATIONS,
    )
    created = base_repository.create_work(work_id="work_001", title="Stay Safe")
    old_manifest = created.manifest_path.read_bytes()
    old_version = created.schema_version
    broken_migrations = _future_work_migration(broken=True)
    target_version = broken_migrations[-1].version

    broken_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=broken_migrations,
    )
    with pytest.raises(WorkUpgradeError, match="failed to upgrade Work"):
        broken_repository.open_work("work_001")

    with repository_read(created.database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    assert version == old_version
    assert created.manifest_path.read_bytes() == old_manifest
    backup = created.database_path.with_name(
        f"{created.database_path.name}.backup-v{old_version}-to-v{target_version}"
    )
    assert backup.is_file()


def test_current_schema_open_uses_fast_verification_without_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_fast", title="Fast Open")

    original_verify = lifecycle_module.verify_work_database_for_open
    calls: list[str] = []

    def verify(*args: object, **kwargs: object):
        calls.append("fast")
        return original_verify(*args, **kwargs)

    def unexpected_migration(*args: object, **kwargs: object):
        raise AssertionError("current-schema open must not enter migration path")

    monkeypatch.setattr(
        lifecycle_module,
        "verify_work_database_for_open",
        verify,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "migrate_work_database",
        unexpected_migration,
    )

    opened = repository.open_work(created.work_id)

    assert opened.work_id == created.work_id
    assert opened.migration_backup_path is None
    assert calls == ["fast"]


def test_current_schema_open_rejects_foreign_key_violation(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_fk", title="Broken FK")

    raw = sqlite3.connect(created.database_path)
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            """
            INSERT INTO entity_revisions (
                id,
                entity_type,
                entity_id,
                entity_revision,
                commit_seq,
                change_kind,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "revision_invalid_fk",
                "test",
                "entity_invalid_fk",
                1,
                999999,
                "corrupt",
                "2026-09-20T00:00:00+00:00",
            ),
        )
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(WorkUpgradeError, match="failed to upgrade Work") as exc_info:
        repository.open_work(created.work_id)

    assert "foreign_key_check failed" in str(exc_info.value)


def test_pending_schema_open_uses_migration_not_fast_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_repository = WorkLifecycleRepository(tmp_path)
    base_repository.create_work(work_id="work_upgrade_path", title="Upgrade Path")
    future_migrations = _future_work_migration()
    repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=future_migrations,
    )

    original_migrate = lifecycle_module.migrate_work_database
    calls: list[str] = []

    def migrate(*args: object, **kwargs: object):
        calls.append("migrate")
        return original_migrate(*args, **kwargs)

    def unexpected_fast_verify(*args: object, **kwargs: object):
        raise AssertionError("pending migration must not use fast-open verification")

    monkeypatch.setattr(lifecycle_module, "migrate_work_database", migrate)
    monkeypatch.setattr(
        lifecycle_module,
        "verify_work_database_for_open",
        unexpected_fast_verify,
    )

    opened = repository.open_work("work_upgrade_path")

    assert opened.schema_version == future_migrations[-1].version
    assert opened.migration_backup_path is not None
    assert opened.migration_backup_path.is_file()
    assert calls == ["migrate"]


def test_open_wraps_migration_drift_as_work_upgrade_error(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Drifted Work")

    with repository_write(created.database_path) as connection:
        connection.execute(
            """
            UPDATE schema_migrations
            SET checksum = 'tampered-checksum'
            WHERE version = (SELECT MAX(version) FROM schema_migrations)
            """
        )

    with pytest.raises(WorkUpgradeError, match="failed to upgrade Work") as exc_info:
        repository.open_work("work_001")

    assert "drift detected" in str(exc_info.value)


def test_legacy_v1_live_manifest_is_normalized_without_hash_enforcement(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path, app_version="new")
    created = repository.create_work(work_id="work_001", title="Legacy Manifest")

    legacy_manifest = json.loads(created.manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["format_version"] = 1
    legacy_manifest["integrity"] = {"database_sha256": "0" * 64}
    created.manifest_path.write_text(
        json.dumps(legacy_manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with repository_write(created.database_path) as connection:
        connection.execute(
            """
            UPDATE work_metadata
            SET title = 'Edited After Legacy Hash',
                current_revision = current_revision + 1
            WHERE work_id = 'work_001'
            """
        )

    opened = repository.open_work("work_001")
    normalized = json.loads(opened.manifest_path.read_text(encoding="utf-8"))

    assert opened.title == "Edited After Legacy Hash"
    assert normalized["format_version"] == 2
    assert normalized["integrity"] == {
        "mode": "live_mutable",
        "database_hash_policy": "package_only",
    }


def test_open_repairs_manifest_left_behind_after_completed_db_upgrade(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Refresh Manifest")
    old_manifest = json.loads(created.manifest_path.read_text(encoding="utf-8"))
    future_migrations = _future_work_migration()

    from manga_autopilot.storage import migrate_work_database

    migrate_work_database(
        created.database_path,
        migrations=future_migrations,
    )

    inspection = inspect_work_directory(
        created.root,
        migrations=future_migrations,
    )
    assert inspection.database_schema_version == future_migrations[-1].version
    assert inspection.manifest_schema_version == old_manifest["work_schema_version"]
    assert inspection.manifest_refresh_required is True

    upgraded_repository = WorkLifecycleRepository(
        tmp_path,
        work_migrations=future_migrations,
    )
    opened = upgraded_repository.open_work("work_001")

    refreshed = json.loads(opened.manifest_path.read_text(encoding="utf-8"))
    assert refreshed["work_schema_version"] == future_migrations[-1].version
    assert opened.migration_backup_path is None


def test_portable_work_inspection_does_not_require_master_database(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path / "library")
    created = repository.create_work(work_id="work_001", title="Portable")

    portable_root = tmp_path / "portable-work"
    shutil.copytree(created.root, portable_root)

    inspection = inspect_work_directory(portable_root)

    assert inspection.work_id == "work_001"
    assert inspection.root == portable_root.resolve()
    assert inspection.database_schema_version == WORK_MIGRATIONS[-1].version
    assert inspection.upgrade_required is False
    assert not (portable_root.parent / "master.sqlite3").exists()



def test_recovery_scan_detects_and_finalizes_complete_stale_staging(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_staged", title="Staged Work")

    staging = tmp_path / "works" / ".creating-work_staged"
    os.replace(created.root, staging)
    with repository_write(repository.paths.master_db) as connection:
        connection.execute(
            "DELETE FROM work_catalog WHERE work_id = 'work_staged'"
        )

    recovery_tree_before_scan = _recovery_tree_snapshot(staging)

    findings = repository.scan_recovery()

    assert _recovery_tree_snapshot(staging) == recovery_tree_before_scan
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "STALE_STAGING_VALID"
    assert finding.work_id == "work_staged"
    assert finding.valid is True
    assert finding.recommended_action == "finalize"
    assert finding.path == staging
    assert finding.diagnostics == ()

    catalog = repository.finalize_staging_work("work_staged")

    assert catalog.work_id == "work_staged"
    assert not staging.exists()
    assert (tmp_path / "works" / "work_staged").is_dir()
    opened = repository.open_work("work_staged")
    assert opened.title == "Staged Work"
    _assert_initial_work_revision(opened.database_path, opened.work_id)
    assert repository.scan_recovery() == ()


def test_recovery_scan_reports_invalid_staging_without_deleting_it(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    staging = tmp_path / "works" / ".creating-work_broken"
    staging.mkdir()
    (staging / "manifest.json").write_text(
        '{"format":"manga-autopilot-work","format_version":2,'
        '"work_id":"work_broken","database":"work.sqlite3",'
        '"work_schema_version":1}',
        encoding="utf-8",
    )
    evidence = staging / "partial.txt"
    evidence.write_text("keep recovery evidence", encoding="utf-8")

    findings = repository.scan_recovery()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "STALE_STAGING_INVALID"
    assert finding.work_id == "work_broken"
    assert finding.valid is False
    assert finding.recommended_action == "quarantine"
    assert finding.diagnostics
    assert staging.is_dir()
    assert evidence.read_text(encoding="utf-8") == "keep recovery evidence"


@pytest.mark.parametrize(
    ("recovery_shape", "expected_kind"),
    [
        ("staging", "STALE_STAGING_INVALID"),
        ("orphan", "UNREGISTERED_WORK_INVALID"),
    ],
)
@pytest.mark.parametrize(
    ("damage_kind", "diagnostic_fragment"),
    [
        ("migration_drift", "MigrationDriftError"),
        ("non_prefix_history", "ordered configured prefix"),
        ("foreign_key", "foreign_key_check failed"),
        ("sqlite_corruption", "not a readable SQLite database"),
    ],
)
def test_recovery_rejects_database_damage_normal_open_would_reject(
    tmp_path: Path,
    recovery_shape: str,
    expected_kind: str,
    damage_kind: str,
    diagnostic_fragment: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    work_id = f"work_{recovery_shape}_{damage_kind}"
    created = repository.create_work(work_id=work_id, title="Damaged Recovery")

    with repository_write(repository.paths.master_db) as connection:
        connection.execute(
            "DELETE FROM work_catalog WHERE work_id = ?",
            (work_id,),
        )

    if recovery_shape == "staging":
        recovery_root = tmp_path / "works" / f".creating-{work_id}"
        os.replace(created.root, recovery_root)
    else:
        recovery_root = created.root

    database = recovery_root / "work.sqlite3"
    if damage_kind == "migration_drift":
        with repository_write(database) as connection:
            connection.execute(
                """
                UPDATE schema_migrations
                SET checksum = 'tampered-checksum'
                WHERE version = (SELECT MAX(version) FROM schema_migrations)
                """
            )
    elif damage_kind == "non_prefix_history":
        with repository_write(database) as connection:
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = ?",
                (WORK_MIGRATIONS[0].version,),
            )
    elif damage_kind == "foreign_key":
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO entity_revisions (
                    id,
                    entity_type,
                    entity_id,
                    entity_revision,
                    commit_seq,
                    change_kind,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "revision_corrupt",
                    "test",
                    "entity_corrupt",
                    1,
                    999999,
                    "corrupt",
                    "2026-09-20T00:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()
    else:
        for suffix in ("-wal", "-shm"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        database.write_bytes(b"not a sqlite database")

    recovery_tree_before_scan = _recovery_tree_snapshot(recovery_root)

    findings = repository.scan_recovery()

    assert _recovery_tree_snapshot(recovery_root) == recovery_tree_before_scan
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == expected_kind
    assert finding.work_id == work_id
    assert finding.valid is False
    assert diagnostic_fragment in " ".join(finding.diagnostics)

    if recovery_shape == "staging":
        assert finding.recommended_action == "quarantine"
        with pytest.raises(WorkRecoveryError, match="cannot finalize invalid staging"):
            repository.finalize_staging_work(work_id)
        assert recovery_root.is_dir()
    else:
        assert finding.recommended_action is None
        with pytest.raises(WorkRecoveryError, match="cannot register invalid orphan"):
            repository.reconcile_orphan_work(work_id)
        assert recovery_root.is_dir()


@pytest.mark.parametrize(
    ("recovery_shape", "expected_kind"),
    [
        ("staging", "STALE_STAGING_INVALID"),
        ("orphan", "UNREGISTERED_WORK_INVALID"),
    ],
)
def test_recovery_rejects_unsnapshotted_master_lineage(
    tmp_path: Path,
    recovery_shape: str,
    expected_kind: str,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    work_id = f"work_linked_{recovery_shape}"
    created = repository.create_work(work_id=work_id, title="Linked Recovery")

    with repository_write(created.database_path) as connection:
        connection.execute(
            """
            UPDATE work_metadata
            SET universe_source_id = 'universe_legacy'
            WHERE work_id = ?
            """,
            (work_id,),
        )
    with repository_write(repository.paths.master_db) as connection:
        connection.execute(
            "DELETE FROM work_catalog WHERE work_id = ?",
            (work_id,),
        )

    if recovery_shape == "staging":
        recovery_root = tmp_path / "works" / f".creating-{work_id}"
        os.replace(created.root, recovery_root)
    else:
        recovery_root = created.root

    recovery_tree_before_scan = _recovery_tree_snapshot(recovery_root)
    findings = repository.scan_recovery()

    assert _recovery_tree_snapshot(recovery_root) == recovery_tree_before_scan
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == expected_kind
    assert finding.valid is False
    assert "immutable source snapshots" in " ".join(finding.diagnostics)

    if recovery_shape == "staging":
        with pytest.raises(WorkRecoveryError, match="invalid staging"):
            repository.finalize_staging_work(work_id)
    else:
        with pytest.raises(WorkRecoveryError, match="invalid orphan"):
            repository.reconcile_orphan_work(work_id)


def test_quarantine_invalid_staging_preserves_evidence_and_is_idempotent_for_scan(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    staging = tmp_path / "works" / ".creating-work_broken"
    staging.mkdir()
    (staging / "partial.txt").write_text("evidence", encoding="utf-8")

    destination = repository.quarantine_staging_work(
        "work_broken",
        reason="incomplete crash state",
    )

    assert not staging.exists()
    assert destination.is_dir()
    assert (destination / "partial.txt").read_text(encoding="utf-8") == "evidence"

    receipt = destination.with_name(destination.name + ".recovery.json")
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["work_id"] == "work_broken"
    assert payload["source"] == ".creating-work_broken"
    assert payload["reason"] == "incomplete crash state"
    assert payload["quarantined_at"]

    assert repository.scan_recovery() == ()


def test_finalize_staging_is_idempotent_after_directory_move(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_retry", title="Retry Finalize")

    staging = tmp_path / "works" / ".creating-work_retry"
    os.replace(created.root, staging)
    with repository_write(repository.paths.master_db) as connection:
        connection.execute("DELETE FROM work_catalog WHERE work_id = 'work_retry'")

    first = repository.finalize_staging_work("work_retry")
    second = repository.finalize_staging_work("work_retry")

    assert first.work_id == "work_retry"
    assert second == first
    assert (tmp_path / "works" / "work_retry").is_dir()
    assert not staging.exists()



def test_repository_rejects_symlinked_master_database_before_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-master.sqlite3"
    bootstrap_master_database(outside, database_id="outside_master")
    before = outside.read_bytes()

    storage = tmp_path / "library"
    (storage / "works").mkdir(parents=True)
    (storage / "shared_assets").mkdir()
    (storage / "projects").mkdir()
    link = storage / "master.sqlite3"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        WorkLifecycleRepository(storage)

    assert outside.read_bytes() == before


def test_open_rejects_symlinked_work_manifest_without_reading_external_file(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Safe")
    outside = tmp_path / "outside-manifest.json"
    outside.write_text(created.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    before = outside.read_bytes()

    created.manifest_path.unlink()
    try:
        created.manifest_path.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        repository.open_work("work_001")

    assert outside.read_bytes() == before


def test_open_rejects_symlinked_work_database_without_mutating_external_db(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_001", title="Safe")
    outside = tmp_path / "outside-work.sqlite3"
    shutil.copy2(created.database_path, outside)
    before = outside.read_bytes()

    created.database_path.unlink()
    try:
        created.database_path.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(UnsafeStoragePathError, match="symlink"):
        repository.open_work("work_001")

    assert outside.read_bytes() == before


def test_recovery_refuses_orphan_with_symlinked_critical_database(
    tmp_path: Path,
) -> None:
    repository = WorkLifecycleRepository(tmp_path)
    created = repository.create_work(work_id="work_orphan", title="Orphan")
    with repository_write(repository.paths.master_db) as connection:
        connection.execute("DELETE FROM work_catalog WHERE work_id = 'work_orphan'")

    outside = tmp_path / "outside-orphan.sqlite3"
    shutil.copy2(created.database_path, outside)
    before = outside.read_bytes()
    created.database_path.unlink()
    try:
        created.database_path.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    findings = repository.scan_recovery()

    assert len(findings) == 1
    assert findings[0].kind == "UNREGISTERED_WORK_INVALID"
    assert findings[0].valid is False
    with pytest.raises(WorkRecoveryError, match="invalid orphan"):
        repository.reconcile_orphan_work("work_orphan")
    assert outside.read_bytes() == before
