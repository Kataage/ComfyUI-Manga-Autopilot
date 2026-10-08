"""Work Artifact metadata / immutable local publication safety regression tests."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

import manga_autopilot.repositories.artifacts as artifacts_module
from manga_autopilot.repositories import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRepository,
    WorkLifecycleRepository,
)
from manga_autopilot.storage import (
    WORK_MIGRATIONS,
    bootstrap_work_database,
    migrate_work_database,
    read_work_identity,
    repository_read,
)


@pytest.fixture
def work(tmp_path: Path):
    handle = WorkLifecycleRepository(tmp_path).create_work(
        title="Artifact safety", work_id="work_artifact_test"
    )
    return ArtifactRepository(tmp_path, handle.work_id), handle


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (9, 7), "white").save(output, format="PNG")
    return output.getvalue()


def _counts(path: Path) -> tuple[int, int, int]:
    with repository_read(path) as db:
        return tuple(
            db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("artifacts", "commits", "entity_revisions")
        )


def _register(repo: ArtifactRepository, **extra):
    kwargs = {
        "data": _png(),
        "relative_path": "assets/panels/reveal.png",
        "artifact_type": "panel_candidate",
        "scope_type": "panel",
        "scope_id": "panel_001",
        "dependency_fingerprint": "gen-v1:abc123",
        "mime_type": "image/png",
    }
    kwargs.update(extra)
    return repo.register_local_bytes(**kwargs)


def test_w0005_schema_contract_indexes_and_fk(work):
    _, handle = work
    with repository_read(handle.database_path) as db:
        version = db.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        columns = {
            item["name"] for item in db.execute("PRAGMA table_info(artifacts)")
        }
        indexes = {
            item["name"] for item in db.execute("PRAGMA index_list(artifacts)")
        }
        fks = {
            item["table"] for item in db.execute("PRAGMA foreign_key_list(artifacts)")
        }
    assert version >= 5
    assert {
        "id", "artifact_type", "scope_type", "scope_id", "relative_path",
        "mime_type", "sha256", "file_size", "width", "height", "run_id",
        "generation_attempt_id", "revision", "dependency_fingerprint",
        "status", "created_commit_seq", "created_at", "archived_at",
    } <= columns
    assert {
        "idx_artifacts_scope_type", "idx_artifacts_sha256",
        "idx_artifacts_run_id", "idx_artifacts_status",
    } <= indexes
    # The immutable W0005 Artifact schema stores Run/Attempt references as
    # text; adding W0006 Runs does not retroactively change its foreign keys.
    assert fks == {"commits"}


def test_published_image_is_work_relative_hashed_and_historically_queryable(work):
    repo, handle = work
    original_counts = _counts(handle.database_path)
    result = _register(repo, artifact_id="artifact_one", run_id="planned_run",
                       generation_attempt_id="attempt_future")
    destination = handle.root / result["relative_path"]
    assert destination.is_file()
    assert destination.read_bytes() == _png()
    assert result["relative_path"] == "assets/panels/reveal.png"
    assert result["sha256"] == hashlib.sha256(_png()).hexdigest()
    assert result["file_size"] == len(_png())
    assert result["width"] == 9
    assert result["height"] == 7
    assert result["mime_type"] == "image/png"
    assert result["artifact_type"] == "panel_candidate"
    assert result["dependency_fingerprint"] == "gen-v1:abc123"
    assert result["status"] == "READY"
    assert result["revision"] == 1
    assert result["created_commit_seq"] > 0
    assert result["run_id"] == "planned_run"
    assert result["generation_attempt_id"] == "attempt_future"
    assert repo.get("artifact_one") == result
    assert repo.list_for_scope("panel", "panel_001") == [result]
    assert repo.verify_registered_file("artifact_one") == result
    assert _counts(handle.database_path) == (
        original_counts[0] + 1, original_counts[1] + 1,
        original_counts[2] + 1,
    )
    with repository_read(handle.database_path) as db:
        revision = db.execute(
            """SELECT * FROM entity_revisions
            WHERE entity_type = 'artifact' AND entity_id = ?""",
            ("artifact_one",),
        ).fetchone()
        commit = db.execute(
            "SELECT operation_type FROM commits WHERE commit_seq = ?",
            (result["created_commit_seq"],),
        ).fetchone()
    assert revision["entity_revision"] == 1
    assert revision["commit_seq"] == result["created_commit_seq"]
    assert revision["before_json"] is None
    assert json.loads(revision["after_json"]) == result
    assert commit["operation_type"] == "register_artifact"
    assert sorted((handle.root / "assets" / "temp").iterdir()) == []


def test_short_commit_sees_complete_published_file_not_temporary(work, monkeypatch):
    repo, handle = work
    original = artifacts_module.create_work_commit
    called = []

    def assert_file_ready(conn, **kwargs):
        target = handle.root / "exports" / "pages" / "final.png"
        assert target.is_file()
        assert target.read_bytes() == _png()
        assert not list((handle.root / "assets" / "temp").iterdir())
        # DB has not yet inserted its formal Artifact row.
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        called.append(target)
        return original(conn, **kwargs)

    monkeypatch.setattr(artifacts_module, "create_work_commit", assert_file_ready)
    result = _register(
        repo, relative_path="exports/pages/final.png", artifact_type="page_render"
    )
    assert result["id"]
    assert called == [handle.root / "exports" / "pages" / "final.png"]


def test_commit_guard_rejects_ready_artifact_atomically_after_file_publish(work):
    repo, handle = work
    baseline = _counts(handle.database_path)
    checked = []

    def fail_under_write_lock(conn):
        assert conn.in_transaction
        # No formal Artifact commit, row, or history exists yet.
        assert _counts(handle.database_path) == baseline
        checked.append(True)
        raise RuntimeError("source revision changed")

    source = handle.root / "source.png"
    source.write_bytes(_png())
    with pytest.raises(RuntimeError, match="source revision changed"):
        repo.register_local_file(
            source_path=source,
            relative_path="assets/panels/reveal.png",
            artifact_type="panel_candidate",
            scope_type="panel", scope_id="panel_001",
            mime_type="image/png",
            dependency_fingerprint="gen-v1:abc123",
            commit_guard=fail_under_write_lock,
        )
    assert checked == [True]
    assert _counts(handle.database_path) == baseline
    # The hard-linked final file remains orphaned for crash-safe recovery.
    assert (handle.root / "assets/panels/reveal.png").is_file()


def test_streaming_file_registration_preserves_source_and_payload(work, tmp_path):
    repo, handle = work
    source = tmp_path / "already-rendered.bin"
    data = b"rendered artifact payload" * 400
    source.write_bytes(data)
    row = repo.register_local_file(
        source_path=source,
        relative_path="assets/pages/generated.bin",
        artifact_type="page_render",
        scope_type="page",
        scope_id="page_1",
        dependency_fingerprint="plan:7",
    )
    assert source.read_bytes() == data
    assert (handle.root / row["relative_path"]).read_bytes() == data
    assert row["file_size"] == len(data)
    assert row["sha256"] == hashlib.sha256(data).hexdigest()
    assert row["width"] is None and row["height"] is None


def test_duplicate_final_path_is_immutable_and_does_not_create_new_rows(work):
    repo, handle = work
    first = _register(repo)
    count = _counts(handle.database_path)
    with pytest.raises(FileExistsError):
        _register(repo, artifact_id="second", data=b"different data",
                  mime_type="application/octet-stream")
    assert (handle.root / first["relative_path"]).read_bytes() == _png()
    assert _counts(handle.database_path) == count
    with pytest.raises(ArtifactNotFoundError):
        repo.get("second")


def test_duplicate_artifact_id_leaves_orphan_final_file_after_db_rollback(work):
    repo, handle = work
    original = _register(repo, artifact_id="artifact_repeat")
    count = _counts(handle.database_path)
    orphan = "assets/panels/new_filename.png"
    with pytest.raises(sqlite3.IntegrityError):
        _register(repo, artifact_id="artifact_repeat", relative_path=orphan)
    assert _counts(handle.database_path) == count
    assert repo.get("artifact_repeat") == original
    assert (handle.root / orphan).read_bytes() == _png()
    assert not list((handle.root / "assets" / "temp").iterdir())


def test_simulated_failed_db_commit_never_inserts_row(work, monkeypatch):
    repo, handle = work
    count = _counts(handle.database_path)

    def fail_commit(*args, **kwargs):
        raise RuntimeError("crash before registration")

    monkeypatch.setattr(artifacts_module, "create_work_commit", fail_commit)
    with pytest.raises(RuntimeError, match="crash before registration"):
        _register(repo)
    assert _counts(handle.database_path) == count
    assert (handle.root / "assets/panels/reveal.png").is_file()
    assert not list((handle.root / "assets" / "temp").iterdir())


def test_publication_failure_cannot_create_a_row_and_cleans_temp(work, monkeypatch):
    repo, handle = work
    count = _counts(handle.database_path)

    def fail_publish(*args, **kwargs):
        raise OSError("cannot publish file")

    monkeypatch.setattr(artifacts_module, "_publish_exclusive", fail_publish)
    with pytest.raises(OSError, match="cannot publish file"):
        _register(repo)
    assert _counts(handle.database_path) == count
    assert not (handle.root / "assets/panels/reveal.png").exists()
    assert not list((handle.root / "assets" / "temp").iterdir())


def test_invalid_or_empty_media_rejected_without_publication(work):
    repo, handle = work
    original = _counts(handle.database_path)
    for payload in (b"", b"this is not a PNG"):
        with pytest.raises(ArtifactIntegrityError):
            _register(repo, data=payload)
    with pytest.raises(ArtifactIntegrityError, match="does not match"):
        _register(repo, mime_type="image/jpeg")
    with pytest.raises(ArtifactIntegrityError, match="unsupported image"):
        _register(repo, mime_type="image/tiff")
    assert _counts(handle.database_path) == original
    assert not (handle.root / "assets/panels/reveal.png").exists()
    assert not list((handle.root / "assets" / "temp").iterdir())


@pytest.mark.parametrize("malicious", [
    "../escape.png", "/tmp/evil.png", "C:/evil.png", "assets/../escape.png",
    "assets//nested.png", "assets/panels/../../evil.png",
    "assets\\panels\\foo.png", "assets/temp/candidate.png",
    "work.sqlite3", "manifest.json", "assets/panels/CON.txt",
    "assets/panels/COM1.png", "assets/panels/trailing.",
    "assets/panels/", "assets/.hidden.png",
    "exports/pdf/evil\x00.pdf",
])
def test_invalid_paths_are_rejected_without_any_db_write(work, malicious):
    repo, handle = work
    original = _counts(handle.database_path)
    with pytest.raises(ValueError):
        _register(repo, relative_path=malicious)
    assert _counts(handle.database_path) == original


def test_symlink_directory_and_final_symlink_are_rejected(work, tmp_path):
    repo, handle = work
    outside = tmp_path / "outside"
    outside.mkdir()
    (handle.root / "assets" / "panels").mkdir()
    try:
        (handle.root / "assets" / "panels" / "external").symlink_to(
            outside, target_is_directory=True
        )
        (handle.root / "assets" / "panels" / "linked.png").symlink_to(
            outside / "link_target.png"
        )
    except OSError as exc:
        pytest.skip(f"symlink privilege unsupported on this runner: {exc}")
    original = _counts(handle.database_path)
    with pytest.raises(ValueError):
        _register(repo, relative_path="assets/panels/external/out.png")
    with pytest.raises(ValueError):
        _register(repo, relative_path="assets/panels/linked.png")
    assert _counts(handle.database_path) == original
    assert not list(outside.iterdir())


def test_modified_or_missing_registered_file_is_reported_but_history_retained(work):
    repo, handle = work
    row = _register(repo)
    full_path = handle.root / row["relative_path"]
    full_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="hash/size mismatch"):
        repo.verify_registered_file(row["id"])
    assert repo.get(row["id"]) == row
    full_path.unlink()
    with pytest.raises(ArtifactIntegrityError, match="missing"):
        repo.verify_registered_file(row["id"])
    assert repo.get(row["id"]) == row


def test_invalid_scope_fingerprint_and_source_are_rejected(work, tmp_path):
    repo, handle = work
    count = _counts(handle.database_path)
    with pytest.raises(ValueError, match="together"):
        _register(repo, scope_id=None)
    with pytest.raises(ValueError, match="fingerprint"):
        _register(repo, dependency_fingerprint="")
    with pytest.raises(ValueError, match="artifact_type"):
        _register(repo, artifact_type="  ")
    with pytest.raises(ValueError, match="source_path"):
        repo.register_local_file(
            source_path=tmp_path / "missing",
            relative_path="assets/panels/anything.png",
            artifact_type="panel_candidate",
            dependency_fingerprint="x",
        )
    assert _counts(handle.database_path) == count


def test_w0004_to_w0005_upgrade_preserves_identity_backup_and_history(tmp_path):
    old = tmp_path / "historic.sqlite3"
    initial = bootstrap_work_database(
        old, work_id="historic_work", database_id="historic_db_001",
        migrations=WORK_MIGRATIONS[:4],
    )
    assert initial.migration.current_version == 4
    with repository_read(old) as db:
        w4 = db.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 4"
        ).fetchone()[0]
    upgraded = migrate_work_database(
        old, work_id="historic_work", migrations=WORK_MIGRATIONS[:5]
    )
    assert upgraded.applied_versions == (5,)
    assert upgraded.backup_path is not None
    assert upgraded.backup_path.is_file()
    assert read_work_identity(old).database_id == "historic_db_001"
    with repository_read(old) as db:
        assert db.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 4"
        ).fetchone()[0] == w4
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'invalidations'"
        ).fetchone() is not None
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'artifacts'"
        ).fetchone() is not None
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    again = migrate_work_database(
        old, work_id="historic_work", migrations=WORK_MIGRATIONS[:5]
    )
    assert again.applied_versions == ()
    assert again.backup_path is None
