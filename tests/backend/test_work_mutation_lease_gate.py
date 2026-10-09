"""Cross-surface exclusive Work writer tests for Phase C Issue #374."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from manga_autopilot.repositories.artifacts import ArtifactRepository
from manga_autopilot.repositories.durable_runs import (
    DurableRunRepository,
    WorkLeaseConflictError,
)
from manga_autopilot.repositories.page_domain import (
    LayoutRepository,
    PageRepository,
    PanelRepository,
)
from manga_autopilot.repositories.work_lifecycle import WorkLifecycleRepository
from manga_autopilot.services.autopilot import OrchestratorHooks
from manga_autopilot.services.durable_autopilot import DurableAutopilotOrchestrator
from manga_autopilot.storage.repository import (
    WorkMutationLeaseConflictError,
    owned_work_mutation,
    repository_write,
)


def test_work_domain_mutations_require_live_lease_owner_and_explicit_recovery(
    tmp_path: Path,
) -> None:
    lifecycle = WorkLifecycleRepository(tmp_path)
    first = lifecycle.create_work(title="Leased", work_id="work_lease_one")
    second = lifecycle.create_work(title="Independent", work_id="work_lease_two")
    pages = PageRepository(first.database_path)
    layouts = LayoutRepository(first.database_path)
    panels = PanelRepository(first.database_path)
    other = PageRepository(second.database_path)
    repo = DurableRunRepository(first.database_path)

    page = pages.create_page(
        page_id="p0", page_number=1, order_key="1",
        page_purpose="original",
    )
    layout = layouts.create_layout(layout_id="l0", page_id=page["id"])
    slot = layouts.create_slot(
        slot_id="s0", layout_id=layout["id"], slot_key="0",
        reading_order=1, geometry={"x": 0},
    )
    panel = panels.create_panel(
        panel_id="panel0", page_id=page["id"],
        order_index=1, panel_purpose="original", layout_slot_id=slot["id"],
    )
    repo.acquire_lease(
        work_id=first.work_id, lease_owner="old",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60,
    )

    # Independent Work operations must never be blocked by another Work's lease.
    other.create_page(page_id="other", page_number=1, order_key="1",
                      page_purpose="independent")

    with pytest.raises(WorkMutationLeaseConflictError):
        pages.update_page("p0", expected_revision=page["revision"],
                          page_purpose="stale human edit")
    with pytest.raises(WorkMutationLeaseConflictError):
        pages.create_page(page_id="p1", page_number=2, order_key="2",
                          page_purpose="unowned")
    with pytest.raises(WorkMutationLeaseConflictError):
        layouts.update_layout("l0", expected_revision=layout["revision"],
                              geometry_json={"x": 10})
    with pytest.raises(WorkMutationLeaseConflictError):
        layouts.update_slot("s0", expected_revision=slot["revision"],
                            geometry_json={"x": 10})
    with pytest.raises(WorkMutationLeaseConflictError):
        panels.update_panel("panel0", expected_revision=panel["revision"],
                            panel_purpose="wrong")
    with pytest.raises(WorkMutationLeaseConflictError):
        ArtifactRepository(tmp_path, first.work_id).register_local_bytes(
            data=b"human artifact", relative_path="assets/data/human.txt",
            artifact_type="generation_log", dependency_fingerprint="test",
        )

    # Trusted internal write paths can act only with the matching live token.
    current_page_revision = pages.get_page("p0")["revision"]
    with owned_work_mutation(first.work_id, "old"):
        own_page = pages.update_page(
            "p0", expected_revision=current_page_revision,
            page_purpose="writer owned",
        )
    assert own_page["revision"] == current_page_revision + 1
    with owned_work_mutation(first.work_id, "not-old"):
        with pytest.raises(WorkMutationLeaseConflictError):
            pages.create_page(page_id="bad", page_number=2, order_key="2",
                              page_purpose="spoofed")
    with owned_work_mutation(second.work_id, "old"):
        with pytest.raises(WorkMutationLeaseConflictError):
            pages.create_page(page_id="bad2", page_number=2, order_key="2",
                              page_purpose="wrong Work")

    # An expired lease is deliberately not silently ignored by a human edit;
    # recovery must explicitly reclaim the old token first.
    expired = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    with repository_write(first.database_path) as db:
        db.execute(
            "UPDATE work_leases SET expires_at = ? WHERE work_id = ?",
            (expired, first.work_id),
        )
    with pytest.raises(WorkMutationLeaseConflictError):
        pages.create_page(page_id="expired", page_number=2, order_key="2",
                          page_purpose="blind edit")
    with owned_work_mutation(first.work_id, "old"):
        with pytest.raises(WorkMutationLeaseConflictError):
            pages.create_page(page_id="expired2", page_number=2, order_key="2",
                              page_purpose="expired owner")

    with pytest.raises(WorkLeaseConflictError, match="explicit"):
        repo.acquire_lease(
            work_id=first.work_id, lease_owner="third",
            lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60,
        )
    repo.acquire_lease(
        work_id=first.work_id, lease_owner="new",
        lease_kind="AUTOPILOT_MUTATION", ttl_seconds=60,
        reclaim_expired_owner="old",
    )
    with owned_work_mutation(first.work_id, "old"):
        with pytest.raises(WorkMutationLeaseConflictError):
            pages.create_page(page_id="old_stale", page_number=2,
                              order_key="2", page_purpose="stale")
    with owned_work_mutation(first.work_id, "new"):
        pages.create_page(page_id="new_owner", page_number=2, order_key="2",
                          page_purpose="reconciled")
    repo.release_lease(work_id=first.work_id, lease_owner="new")

    # A leaked task context must be rejected even after the lease is released.
    with owned_work_mutation(first.work_id, "new"):
        with pytest.raises(WorkMutationLeaseConflictError):
            pages.create_page(page_id="new_stale", page_number=3,
                              order_key="3", page_purpose="after release")
    pages.create_page(page_id="human_after", page_number=3, order_key="3",
                      page_purpose="resumed operator")
    assert pages.get_page("p0")["page_purpose"] == "writer owned"


@pytest.mark.asyncio
async def test_sync_durable_hook_can_write_pages_and_artifacts_with_current_token(
    tmp_path: Path,
) -> None:
    """The owned sync hook runs in asyncio.to_thread, so context must copy."""
    lifecycle = WorkLifecycleRepository(tmp_path)
    handle = lifecycle.create_work(title="Owned generated", work_id="work_sync_lease")
    runs = DurableRunRepository(handle.database_path)
    run_id = runs.create_run(
        run_kind="AUTOPILOT", scope_type="WORK", scope_id=handle.work_id,
        requested_by="owned sync hook", input_fingerprint="same",
    )["id"]

    def generate_page(_run):
        pages = PageRepository(handle.database_path)
        page = pages.create_page(
            page_id="generated", page_number=1, order_key="001",
            page_purpose="generated by the owning hook",
        )
        artifact = ArtifactRepository(tmp_path, handle.work_id).register_local_bytes(
            data=b"generated bytes", relative_path="assets/data/generated.txt",
            artifact_type="generation_log", dependency_fingerprint="test",
            scope_type="page", scope_id=page["id"], run_id=run_id,
        )
        return {"page_id": page["id"], "artifact_id": artifact["id"]}

    result = await DurableAutopilotOrchestrator(
        repository=runs, work_id=handle.work_id,
        hooks=OrchestratorHooks(generate_panels=generate_page),
        allow_omitted_hooks=True, lease_ttl_seconds=9,
    ).execute(run_id, input_payload={}, step_inputs={}, lease_owner="real_hook_owner")
    assert result.machine.state.value == "COMPLETED"
    assert runs.get_run(run_id)["status"] == "COMPLETED"
    assert runs.inspect_lease(handle.work_id) is None
    assert PageRepository(handle.database_path).get_page("generated")["id"] == "generated"
    assert ArtifactRepository(tmp_path, handle.work_id).get(
        result.artefacts["generate_panels"]["artifact_id"],
    )["scope_id"] == "generated"
