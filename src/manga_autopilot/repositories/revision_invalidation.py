"""Auditable Work Page-domain revisions and targeted downstream invalidations.

Use with page_domain mutations inside the same repository_write transaction.
This first dependency slice intentionally stops short of Story/Temporal traversal,
derived-artifact resolution, and dependency-edge fingerprints.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from manga_autopilot.primitives import new_id
from manga_autopilot.storage.repository import (
    create_work_entity_revision,
    repository_read,
)

_DOMAIN_TYPES = {
    "pages": "page",
    "layout_instances": "layout_instance",
    "layout_slots": "layout_slot",
    "panels": "panel",
}
_NARRATIVE_PANEL_FIELDS = frozenset({
    "panel_role",
    "panel_purpose",
    "entry_anchor_id",
    "exit_anchor_id",
    "action_json",
    "camera_json",
    "emotion_requirements_json",
    "environment_requirements_json",
    "continuity_requirements_json",
    "generation_spec_json",
})


def _page_id(
    connection: sqlite3.Connection,
    *,
    table: str,
    state: Mapping[str, Any],
) -> str:
    if table == "pages":
        return str(state["id"])
    if table in {"layout_instances", "panels"}:
        return str(state["page_id"])
    if table == "layout_slots":
        row = connection.execute(
            "SELECT page_id FROM layout_instances WHERE id = ?",
            (state["layout_instance_id"],),
        ).fetchone()
        if row is None:
            raise ValueError("layout slot has no owning Page")
        return str(row["page_id"])
    raise ValueError(f"unknown Page domain table: {table}")


def _affected_targets(
    *,
    table: str,
    state: Mapping[str, Any],
    page_id: str,
    changed_fields: Collection[str],
) -> tuple[tuple[str, str, str], ...]:
    """Return (target_type, target_id, reason) for this *actual* change."""
    page_render = ("page_render", page_id, "page presentation changed")
    page_export = ("page_export", page_id, "page export inputs changed")
    if table != "panels":
        # Page edits and geometry/semantics of Layout/Slots never invalidate
        # an image generation just because its placement or styling changed.
        return (page_render, page_export)

    panel_id = str(state["id"])
    fields = set(changed_fields)
    if fields & _NARRATIVE_PANEL_FIELDS:
        return (
            ("panel_generation_spec", panel_id, "panel narrative intent changed"),
            ("panel_generation_candidates", panel_id, "panel narrative intent changed"),
            ("panel_selected_candidate_validity", panel_id, "panel narrative intent changed"),
            ("panel_qa", panel_id, "panel narrative intent changed"),
            page_render,
            ("page_visual_qa", page_id, "panel narrative intent changed"),
            page_export,
        )
    if "selected_candidate_id" in fields:
        return (
            page_render,
            ("page_visual_qa", page_id, "panel selected candidate changed"),
            page_export,
        )
    return (page_render, page_export)


def record_domain_change(
    connection: sqlite3.Connection,
    *,
    table: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    changed_fields: Collection[str],
    commit_seq: int,
    created_at: str,
) -> None:
    """Atomically append entity history and invalidations for a formal edit.

    Call only after the corresponding row was inserted/updated successfully.
    No side effects are emitted for no-op mutations.
    """
    try:
        entity_type = _DOMAIN_TYPES[table]
    except KeyError as exc:
        raise ValueError(f"unsupported Page domain entity: {table}") from exc
    if before is not None and not changed_fields:
        raise ValueError("no-op revision recording is not allowed")

    entity_id = str(after["id"])
    revision = int(after["revision"])
    create_work_entity_revision(
        connection,
        revision_id=new_id("revision"),
        entity_type=entity_type,
        entity_id=entity_id,
        entity_revision=revision,
        commit_seq=commit_seq,
        change_kind="create" if before is None else "update",
        before_state=dict(before) if before is not None else None,
        after_state=dict(after),
        created_at=created_at,
    )

    page_id = _page_id(connection, table=table, state=after)
    targets = _affected_targets(
        table=table,
        state=after,
        page_id=page_id,
        changed_fields=changed_fields,
    )
    for target_type, target_id, reason in targets:
        connection.execute(
            """
            INSERT INTO invalidations (
                id, source_entity_type, source_entity_id, source_revision,
                target_type, target_id, invalidation_kind, reason,
                created_commit_seq, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("invalidation"),
                entity_type,
                entity_id,
                revision,
                target_type,
                target_id,
                "stale",
                reason,
                commit_seq,
                created_at,
            ),
        )


class PageDomainAuditRepository:
    """Read-only history/staleness ledger queries for v2 Page domain operations."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def list_entity_revisions(
        self, entity_type: str, entity_id: str
    ) -> list[dict[str, Any]]:
        if entity_type not in _DOMAIN_TYPES.values():
            raise ValueError(f"unsupported Page domain entity type: {entity_type}")
        if not entity_id:
            raise ValueError("entity_id must be non-empty")
        with repository_read(self.database_path) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM entity_revisions
                    WHERE entity_type = ? AND entity_id = ?
                    ORDER BY entity_revision, commit_seq
                    """,
                    (entity_type, entity_id),
                )
            ]

    def list_invalidations(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        unresolved_only: bool = True,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if target_type is not None:
            conditions.append("target_type = ?")
            params.append(target_type)
        if target_id is not None:
            conditions.append("target_id = ?")
            params.append(target_id)
        if unresolved_only:
            conditions.append("resolved_commit_seq IS NULL")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with repository_read(self.database_path) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM invalidations"
                    + where
                    + " ORDER BY created_commit_seq, id",
                    params,
                )
            ]


__all__ = ["PageDomainAuditRepository", "record_domain_change"]
