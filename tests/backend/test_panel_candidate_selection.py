"""#334: Candidate identity and atomic Panel selection in the Work database.

These tests use real W0005 Work migrations and immutable ArtifactRepository
rows. Raw SQL below is reserved for simulating historical corruption / states
for which no public immutable Artifact mutation API exists.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from manga_autopilot.repositories import (
    ArtifactRepository,
    PageDomainCandidateSelectionError,
    PageRepository,
    PanelRepository,
    WorkLifecycleRepository,
)
from manga_autopilot.storage import (
    RevisionConflictError,
    repository_read,
    repository_write,
)


def _image(format_name: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (12, 12), "#e03930").save(out, format=format_name)
    return out.getvalue()


@pytest.fixture()
def work(tmp_path: Path):
    lifecycle = WorkLifecycleRepository(tmp_path)
    home = lifecycle.create_work(work_id="work_select_a", title="Candidate selection")
    other = lifecycle.create_work(work_id="work_select_b", title="Other Work")
    pages = PageRepository(home.database_path)
    panels = PanelRepository(home.database_path)
    pages.create_page(
        page_id="page_select", page_number=1, order_key="0001",
        page_purpose="Selected images",
    )
    panels.create_panel(
        panel_id="panel_a", page_id="page_select", order_index=1,
        panel_purpose="First",
        action={"pose": "wave"}, generation_spec={"seed": 7},
    )
    panels.create_panel(
        panel_id="panel_b", page_id="page_select", order_index=2,
        panel_purpose="Second",
    )
    files = ArtifactRepository(tmp_path, "work_select_a")

    def register(
        artifact_id: str, panel: str, *,
        artifact_type: str = "panel_candidate",
        scope_type: str = "panel",
        mime_type: str = "image/png",
    ):
        return files.register_local_bytes(
            artifact_id=artifact_id,
            relative_path=f"assets/candidates/{artifact_id}.png",
            artifact_type=artifact_type,
            scope_type=scope_type,
            scope_id=panel,
            mime_type=mime_type,
            data=_image(),
            dependency_fingerprint=f"input:{artifact_id}",
        )

    register("candidate_a", "panel_a")
    register("candidate_b", "panel_b")
    register("not_candidate", "panel_a", artifact_type="page_render")
    register("wrong_scope", "panel_a", scope_type="page")
    register("not_ready", "panel_a")
    register("archived", "panel_a")
    files.register_local_bytes(
        artifact_id="gif_candidate",
        relative_path="assets/candidates/gif_candidate.gif",
        artifact_type="panel_candidate",
        scope_type="panel",
        scope_id="panel_a",
        mime_type="image/gif",
        data=_image("GIF"),
        dependency_fingerprint="input:gif_candidate",
    )
    other_files = ArtifactRepository(tmp_path, "work_select_b")
    other_files.register_local_bytes(
        artifact_id="candidate_from_other_work",
        relative_path="assets/candidates/candidate_from_other_work.png",
        artifact_type="panel_candidate",
        scope_type="panel", scope_id="panel_a",
        mime_type="image/png", data=_image(),
        dependency_fingerprint="foreign",
    )
    # Simulate statuses created in old Work versions. ArtifactRepository
    # publication is immutable and does not expose a status mutation API.
    with repository_write(home.database_path) as db:
        db.execute("UPDATE artifacts SET status = 'FAILED' WHERE id = 'not_ready'")
        db.execute(
            "UPDATE artifacts SET archived_at = ? WHERE id = 'archived'",
            ("2026-01-01T00:00:00Z",),
        )
    return home, other, panels, files


def _audit_snapshot(home, panels: PanelRepository):
    with repository_read(home.database_path) as db:
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("commits", "entity_revisions", "invalidations")
        }
    return (panels.get_panel("panel_a"), counts)


@pytest.mark.parametrize("candidate_id", [
    "missing", "candidate_from_other_work", "candidate_b",
    "not_candidate", "wrong_scope", "not_ready", "archived",
    "gif_candidate", "", "   ", 17, {"artifact": "candidate_a"},
])
def test_invalid_selection_rolls_back_entire_panel_command(work, candidate_id):
    home, _, panels, _ = work
    original = _audit_snapshot(home, panels)
    with pytest.raises(PageDomainCandidateSelectionError, match="Panel 'panel_a'"):
        panels.update_panel(
            "panel_a", expected_revision=original[0]["revision"],
            selected_candidate_id=candidate_id,
            action_json={"pose": "changed simultaneously"},
        )
    assert _audit_snapshot(home, panels) == original
    assert panels.get_panel("panel_b")["selected_candidate_id"] is None


def test_valid_selection_clear_reselection_and_noop_are_revisioned(work):
    home, _, panels, _ = work
    old, counts = _audit_snapshot(home, panels)
    changed = panels.update_panel(
        "panel_a", expected_revision=old["revision"],
        selected_candidate_id="candidate_a",
    )
    assert changed["revision"] == old["revision"] + 1
    assert changed["selected_candidate_id"] == "candidate_a"
    with repository_read(home.database_path) as db:
        invalidations = {
            row["target_type"] for row in db.execute(
                "SELECT target_type FROM invalidations WHERE created_commit_seq = ?",
                (changed["updated_commit_seq"],),
            )
        }
    assert invalidations == {"page_render", "page_visual_qa", "page_export"}
    assert _audit_snapshot(home, panels)[1] == {
        "commits": counts["commits"] + 1,
        "entity_revisions": counts["entity_revisions"] + 1,
        "invalidations": counts["invalidations"] + 3,
    }

    noop_before = _audit_snapshot(home, panels)
    noop = panels.update_panel(
        "panel_a", expected_revision=changed["revision"],
        selected_candidate_id="candidate_a",
    )
    assert noop == changed
    assert _audit_snapshot(home, panels) == noop_before

    cleared = panels.update_panel(
        "panel_a", expected_revision=changed["revision"], selected_candidate_id=None
    )
    assert cleared["selected_candidate_id"] is None
    assert cleared["revision"] == changed["revision"] + 1
    reselected = panels.update_panel(
        "panel_a", expected_revision=cleared["revision"],
        selected_candidate_id="candidate_a",
    )
    assert reselected["selected_candidate_id"] == "candidate_a"


def test_stale_revision_rejected_before_candidate_check(work):
    home, _, panels, _ = work
    before = _audit_snapshot(home, panels)
    with pytest.raises(RevisionConflictError):
        panels.update_panel(
            "panel_a", expected_revision=2,
            selected_candidate_id="candidate_b",
        )
    assert _audit_snapshot(home, panels) == before


def test_historically_invalid_selection_can_be_cleared_without_reimport(work):
    home, _, panels, _ = work
    # Old versions wrote this without checking ownership. Do not pretend the
    # DB was always clean; repair by an ordinary revision-guarded clear.
    with repository_write(home.database_path) as db:
        db.execute(
            "UPDATE panels SET selected_candidate_id = 'candidate_b' "
            "WHERE id = 'panel_a'"
        )
    old = panels.get_panel("panel_a")
    assert old["selected_candidate_id"] == "candidate_b"
    before = _audit_snapshot(home, panels)
    with pytest.raises(PageDomainCandidateSelectionError):
        panels.update_panel(
            "panel_a", expected_revision=old["revision"],
            selected_candidate_id="candidate_b",
        )
    assert _audit_snapshot(home, panels) == before
    # Unrelated metadata edits should not make historic invalid values
    # impossible to repair, and must not silently validate them.
    metadata = panels.update_panel(
        "panel_a", expected_revision=old["revision"], panel_purpose="review needed"
    )
    assert metadata["selected_candidate_id"] == "candidate_b"
    repaired = panels.update_panel(
        "panel_a", expected_revision=metadata["revision"], selected_candidate_id=None
    )
    assert repaired["selected_candidate_id"] is None
    assert repaired["revision"] == metadata["revision"] + 1
