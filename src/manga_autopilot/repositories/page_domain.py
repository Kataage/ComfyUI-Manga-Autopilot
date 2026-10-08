"""Work-local Page, LayoutInstance, LayoutSlot and Panel repositories (v2).

No HTTP, ComfyUI, or legacy JSON-file services are used here. Each public
mutation opens one short BEGIN IMMEDIATE transaction, creates exactly one
Work commit when changes occur, and uses optimistic entity revisions.
Downstream invalidations and entity-revision snapshots belong to issue #236.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from manga_autopilot.primitives import canonical_json, new_id
from manga_autopilot.repositories.revision_invalidation import record_domain_change
from manga_autopilot.storage.repository import (
    PersistenceError,
    assert_expected_revision,
    create_work_commit,
    repository_read,
    repository_write,
)


class PageDomainNotFoundError(PersistenceError):
    """A requested v2 entity was not found in its Work."""


class PageDomainOwnershipError(PersistenceError):
    """An entity reference crosses Page or Layout ownership boundaries."""


class PageDomainArchivedError(PageDomainOwnershipError):
    """Archived Page cannot be edited through active Page Editor commands."""


class PageDomainCandidateSelectionError(PageDomainOwnershipError):
    """A Panel's selected candidate is not its own current image Artifact."""


_PAGE_FIELDS = frozenset({
    "page_number", "order_key", "page_role", "page_purpose",
    "narrative_goal", "format_kind", "status", "archived_at",
})
_LAYOUT_FIELDS = frozenset({
    "source_template_id", "template_snapshot_id", "layout_kind",
    "reading_direction", "parameters_json", "geometry_json", "constraints_json",
})
_SLOT_FIELDS = frozenset({
    "slot_key", "reading_order", "geometry_json", "semantic_json",
    "safe_subject_region_json", "bubble_regions_json", "forbidden_regions_json",
})
_PANEL_FIELDS = frozenset({
    "order_index", "panel_role", "panel_purpose", "entry_anchor_id",
    "exit_anchor_id", "action_json", "camera_json",
    "emotion_requirements_json", "environment_requirements_json",
    "continuity_requirements_json", "generation_spec_json",
    "selected_candidate_id", "status", "archived_at", "layout_slot_id",
})
_JSON_FIELDS = frozenset({
    "parameters_json", "geometry_json", "constraints_json",
    "semantic_json", "safe_subject_region_json", "bubble_regions_json",
    "forbidden_regions_json", "action_json", "camera_json",
    "emotion_requirements_json", "environment_requirements_json",
    "continuity_requirements_json", "generation_spec_json",
})
_TABLES = frozenset({"pages", "layout_instances", "layout_slots", "panels"})


def _id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _positive(value: int, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _normalize(field: str, value: Any) -> Any:
    if field in _JSON_FIELDS:
        if not isinstance(value, (dict, list)):
            raise ValueError(f"{field} must be a JSON object or array")
        return canonical_json(value)
    if field in {"page_number", "order_index", "reading_order"}:
        return _positive(value, field)
    return value


def _values(changes: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"immutable or unknown fields: {sorted(unknown)}")
    return {key: _normalize(key, value) for key, value in changes.items()}


def _read_row(connection: sqlite3.Connection, table: str, entity_id: str) -> dict[str, Any]:
    if table not in _TABLES:
        raise ValueError(f"unknown Work table: {table}")
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise PageDomainNotFoundError(f"{table} entity not found: {entity_id}")
    return dict(row)


def _commit(connection: sqlite3.Connection, operation: str) -> tuple[int, str]:
    commit = create_work_commit(
        connection,
        commit_id=new_id("commit"),
        actor_type="system",
        operation_type=operation,
    )
    return commit.commit_seq, commit.created_at


def _insert(
    connection: sqlite3.Connection,
    table: str,
    entity_id: str,
    attrs: Mapping[str, Any],
    *,
    commit_seq: int,
    timestamp: str,
) -> dict[str, Any]:
    if table not in _TABLES:
        raise ValueError(f"unknown Work table: {table}")
    data = {
        "id": entity_id,
        **attrs,
        "revision": 1,
        "created_commit_seq": commit_seq,
        "updated_commit_seq": commit_seq,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    connection.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks})",
        tuple(data.values()),
    )
    inserted = _read_row(connection, table, entity_id)
    record_domain_change(
        connection,
        table=table,
        before=None,
        after=inserted,
        changed_fields=attrs.keys(),
        commit_seq=commit_seq,
        created_at=timestamp,
    )
    return inserted


def _patch(
    connection: sqlite3.Connection,
    table: str,
    entity_id: str,
    *,
    expected_revision: int,
    attrs: Mapping[str, Any],
    commit_seq: int | None = None,
    timestamp: str | None = None,
) -> tuple[dict[str, Any], bool]:
    existing = _read_row(connection, table, entity_id)
    _positive(expected_revision, "expected_revision")
    assert_expected_revision(
        entity_type=table,
        entity_id=entity_id,
        expected_revision=expected_revision,
        actual_revision=int(existing["revision"]),
    )
    changed = {k: v for k, v in attrs.items() if existing[k] != v}
    if not changed:
        return existing, False
    if commit_seq is None or timestamp is None:
        commit_seq, timestamp = _commit(connection, f"update_{table}")
    new_fields = {
        **changed,
        "revision": expected_revision + 1,
        "updated_commit_seq": commit_seq,
        "updated_at": timestamp,
    }
    assignments = ", ".join(f"{key} = ?" for key in new_fields)
    result = connection.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ? AND revision = ?",
        (*new_fields.values(), entity_id, expected_revision),
    )
    if result.rowcount != 1:
        raise PersistenceError(f"{table} update lost its revision guard: {entity_id}")
    updated = _read_row(connection, table, entity_id)
    record_domain_change(
        connection,
        table=table,
        before=existing,
        after=updated,
        changed_fields=changed.keys(),
        commit_seq=commit_seq,
        created_at=timestamp,
    )
    return updated, True


def _check_slot_page(connection: sqlite3.Connection, slot_id: str, page_id: str) -> None:
    row = connection.execute(
        """
        SELECT li.page_id
        FROM layout_slots ls
        JOIN layout_instances li ON li.id = ls.layout_instance_id
        WHERE ls.id = ?
        """,
        (slot_id,),
    ).fetchone()
    if row is None or row["page_id"] != page_id:
        raise PageDomainOwnershipError(
            f"layout slot {slot_id!r} does not belong to page {page_id!r}"
        )


def _check_page(connection: sqlite3.Connection, page_id: str) -> None:
    _read_row(connection, "pages", page_id)


def _validate_selected_candidate(
    connection: sqlite3.Connection, panel_id: str, candidate_id: Any,
) -> None:
    """Check the temporary W0005 Candidate == Artifact identity contract.

    A selected_candidate_id is either NULL or the exact Artifact ID of a
    current image/png|jpeg|webp panel_candidate *in this same Work database*.
    The repository_write transaction locks the DB before lookup and until the
    Panel revision is committed. No filename search or cross-Work fallback.
    File hash/integrity is separately enforced when the exporter reads bytes.
    """
    if candidate_id is None:
        return
    if type(candidate_id) is not str or not candidate_id.strip():
        raise PageDomainCandidateSelectionError(
            f"Panel {panel_id!r}: selected_candidate_id must be "
            "a non-empty Artifact ID or null"
        )
    row = connection.execute(
        """SELECT artifact_type, scope_type, scope_id, status, archived_at, mime_type
           FROM artifacts WHERE id = ?""", (candidate_id,),
    ).fetchone()
    if row is None or (
        row["artifact_type"] != "panel_candidate"
        or row["scope_type"] != "panel"
        or row["scope_id"] != panel_id
        or row["status"] != "READY"
        or row["archived_at"] is not None
        or row["mime_type"] not in {"image/png", "image/jpeg", "image/webp"}
    ):
        raise PageDomainCandidateSelectionError(
            f"Panel {panel_id!r}: Candidate {candidate_id!r} must be a "
            "current READY image panel_candidate Artifact owned by this "
            "Panel in the same Work (unknown, archived, wrong type, "
            "wrong Work, or wrong owner is not selectable)"
        )


class PageRepository:
    """Work-local Page CRUD without destructive layout updates."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def create_page(
        self,
        *,
        page_id: str,
        page_number: int,
        order_key: str,
        page_purpose: str,
        page_role: str = "STANDARD",
        narrative_goal: str | None = None,
        format_kind: str = "MANGA_PAGE",
        status: str = "PLANNED",
    ) -> dict[str, Any]:
        attrs = _values({
            "page_number": page_number,
            "order_key": order_key,
            "page_role": page_role,
            "page_purpose": page_purpose,
            "narrative_goal": narrative_goal,
            "format_kind": format_kind,
            "status": status,
        }, _PAGE_FIELDS)
        with repository_write(self.database_path) as conn:
            seq, at = _commit(conn, "create_page")
            return _insert(
                conn, "pages", _id(page_id, "page_id"),
                {**attrs, "layout_instance_id": None},
                commit_seq=seq, timestamp=at,
            )

    def get_page(self, page_id: str) -> dict[str, Any]:
        with repository_read(self.database_path) as conn:
            return _read_row(conn, "pages", _id(page_id, "page_id"))

    def list_pages(self) -> list[dict[str, Any]]:
        with repository_read(self.database_path) as conn:
            return [
                dict(row) for row in conn.execute(
                    "SELECT * FROM pages ORDER BY order_key, page_number, id"
                )
            ]

    def update_page(
        self, page_id: str, *, expected_revision: int, **changes: Any
    ) -> dict[str, Any]:
        attrs = _values(changes, _PAGE_FIELDS)
        with repository_write(self.database_path) as conn:
            record, _ = _patch(
                conn, "pages", _id(page_id, "page_id"),
                expected_revision=expected_revision, attrs=attrs,
            )
            return record


class LayoutRepository:
    """An independent LayoutInstance and its stable, non-destructive Slots."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def create_layout(
        self,
        *,
        layout_id: str,
        page_id: str,
        layout_kind: str = "CUSTOM",
        reading_direction: str = "RTL_TOP_TO_BOTTOM",
        source_template_id: str | None = None,
        template_snapshot_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        geometry: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _id(page_id, "page_id")
        _id(layout_id, "layout_id")
        attrs = _values({
            "layout_kind": layout_kind,
            "reading_direction": reading_direction,
            "source_template_id": source_template_id,
            "template_snapshot_id": template_snapshot_id,
            "parameters_json": {} if parameters is None else parameters,
            "geometry_json": {} if geometry is None else geometry,
            "constraints_json": {} if constraints is None else constraints,
        }, _LAYOUT_FIELDS)
        with repository_write(self.database_path) as conn:
            page = _read_row(conn, "pages", page_id)
            if page["layout_instance_id"] is not None:
                raise PageDomainOwnershipError(
                    f"page {page_id!r} already has a layout instance"
                )
            seq, at = _commit(conn, "create_layout")
            record = _insert(
                conn, "layout_instances", layout_id,
                {"page_id": page_id, **attrs},
                commit_seq=seq, timestamp=at,
            )
            # Creating a layout changes the Page's authoritative association.
            _patch(
                conn, "pages", page_id,
                expected_revision=page["revision"],
                attrs={"layout_instance_id": layout_id},
                commit_seq=seq, timestamp=at,
            )
            return record

    def get_layout(self, layout_id: str) -> dict[str, Any]:
        with repository_read(self.database_path) as conn:
            return _read_row(conn, "layout_instances", _id(layout_id, "layout_id"))

    def get_page_layout(self, page_id: str) -> dict[str, Any] | None:
        with repository_read(self.database_path) as conn:
            _check_page(conn, _id(page_id, "page_id"))
            row = conn.execute(
                "SELECT * FROM layout_instances WHERE page_id = ?",
                (page_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_slots(self, layout_id: str) -> list[dict[str, Any]]:
        with repository_read(self.database_path) as conn:
            _read_row(conn, "layout_instances", _id(layout_id, "layout_id"))
            return [
                dict(row) for row in conn.execute(
                    """
                    SELECT * FROM layout_slots WHERE layout_instance_id = ?
                    ORDER BY reading_order, id
                    """,
                    (layout_id,),
                )
            ]

    def create_slot(
        self,
        *,
        slot_id: str,
        layout_id: str,
        slot_key: str,
        reading_order: int,
        geometry: dict[str, Any],
        semantic: dict[str, Any] | None = None,
        safe_subject_region: dict[str, Any] | None = None,
        bubble_regions: list[Any] | None = None,
        forbidden_regions: list[Any] | None = None,
    ) -> dict[str, Any]:
        attrs = _values({
            "slot_key": slot_key,
            "reading_order": reading_order,
            "geometry_json": geometry,
            "semantic_json": {} if semantic is None else semantic,
            "safe_subject_region_json": (
                {} if safe_subject_region is None else safe_subject_region
            ),
            "bubble_regions_json": [] if bubble_regions is None else bubble_regions,
            "forbidden_regions_json": (
                [] if forbidden_regions is None else forbidden_regions
            ),
        }, _SLOT_FIELDS)
        with repository_write(self.database_path) as conn:
            _read_row(conn, "layout_instances", _id(layout_id, "layout_id"))
            seq, at = _commit(conn, "create_layout_slot")
            return _insert(
                conn, "layout_slots", _id(slot_id, "slot_id"),
                {"layout_instance_id": layout_id, **attrs},
                commit_seq=seq, timestamp=at,
            )

    def get_slot(self, slot_id: str) -> dict[str, Any]:
        with repository_read(self.database_path) as conn:
            return _read_row(conn, "layout_slots", _id(slot_id, "slot_id"))

    def update_slot(
        self, slot_id: str, *, expected_revision: int, **changes: Any
    ) -> dict[str, Any]:
        attrs = _values(changes, _SLOT_FIELDS)
        with repository_write(self.database_path) as conn:
            record, _ = _patch(
                conn, "layout_slots", _id(slot_id, "slot_id"),
                expected_revision=expected_revision, attrs=attrs,
            )
            return record

    def update_layout(
        self,
        layout_id: str,
        *,
        expected_revision: int,
        slot_updates: Sequence[Mapping[str, Any]] = (),
        panel_bindings: Sequence[Mapping[str, Any]] = (),
        **changes: Any,
    ) -> dict[str, Any]:
        """Atomically mutate selected geometry and Panel-Slot bindings.

        Every slot update needs id + expected_revision. Optional layout_id
        and page_id are explicit ownership assertions (never mutable).
        Any failed ownership check or stale revision rolls back all changes.
        """
        attrs = _values(changes, _LAYOUT_FIELDS)
        with repository_write(self.database_path) as conn:
            layout = _read_row(conn, "layout_instances", _id(layout_id, "layout_id"))
            owning_page = _read_row(conn, "pages", layout["page_id"])
            if owning_page["archived_at"] is not None:
                raise PageDomainArchivedError(
                    f"Page {layout['page_id']} is archived; unarchive before editing."
                )
            _positive(expected_revision, "expected_revision")
            assert_expected_revision(
                entity_type="layout_instances",
                entity_id=layout_id,
                expected_revision=expected_revision,
                actual_revision=layout["revision"],
            )
            staged: list[tuple[str, int, dict[str, Any]]] = []
            seen: set[str] = set()
            for update in slot_updates:
                item = dict(update)
                slot_id = _id(item.pop("id", None), "slot id")
                slot_revision = _positive(
                    item.pop("expected_revision", None), "slot expected_revision"
                )
                owner_layout = item.pop("layout_id", layout_id)
                owner_page = item.pop("page_id", layout["page_id"])
                if slot_id in seen:
                    raise ValueError(f"duplicate slot update: {slot_id}")
                seen.add(slot_id)
                row = _read_row(conn, "layout_slots", slot_id)
                if (
                    row["layout_instance_id"] != layout_id
                    or owner_layout != layout_id
                    or owner_page != layout["page_id"]
                ):
                    raise PageDomainOwnershipError(
                        f"slot {slot_id!r} does not belong to layout {layout_id!r}"
                    )
                assert_expected_revision(
                    entity_type="layout_slots", entity_id=slot_id,
                    expected_revision=slot_revision, actual_revision=row["revision"],
                )
                staged.append((slot_id, slot_revision, _values(item, _SLOT_FIELDS)))

            # Bindings are semantic Panel updates, not Panel replacements. Validate
            # every participant before the first write so an ownership/revision
            # error rejects the whole command. A concurrent writer cannot slip in
            # because repository_write holds BEGIN IMMEDIATE for this transaction.
            staged_bindings: list[tuple[str, int, str | None]] = []
            bound: set[str] = set()
            for binding in panel_bindings:
                item = dict(binding)
                panel_id = _id(item.pop("id", None), "panel id")
                panel_revision = _positive(
                    item.pop("expected_revision", None), "panel expected_revision"
                )
                if "layout_slot_id" not in item:
                    raise ValueError("panel binding requires layout_slot_id")
                slot_binding = item.pop("layout_slot_id")
                asserted_page = item.pop("page_id", layout["page_id"])
                if item:
                    raise ValueError(f"unsupported panel binding fields: {sorted(item)}")
                if panel_id in bound:
                    raise ValueError(f"duplicate panel binding: {panel_id}")
                bound.add(panel_id)
                panel = _read_row(conn, "panels", panel_id)
                if panel["page_id"] != layout["page_id"] or asserted_page != layout["page_id"]:
                    raise PageDomainOwnershipError(
                        f"panel {panel_id!r} does not belong to layout Page"
                    )
                if slot_binding is not None:
                    _check_slot_page(
                        conn, _id(slot_binding, "layout_slot_id"), layout["page_id"]
                    )
                assert_expected_revision(
                    entity_type="panels", entity_id=panel_id,
                    expected_revision=panel_revision, actual_revision=panel["revision"],
                )
                staged_bindings.append((panel_id, panel_revision, slot_binding))

            layout_changed = any(layout[k] != v for k, v in attrs.items())
            slots_changed = any(
                any(_read_row(conn, "layout_slots", sid)[k] != v for k, v in values.items())
                for sid, _, values in staged
            )
            bindings_changed = any(
                _read_row(conn, "panels", pid)["layout_slot_id"] != slot_id
                for pid, _, slot_id in staged_bindings
            )
            if not layout_changed and not slots_changed and not bindings_changed:
                return layout
            seq, at = _commit(conn, "update_layout")
            if layout_changed:
                layout, _ = _patch(
                    conn, "layout_instances", layout_id,
                    expected_revision=expected_revision, attrs=attrs,
                    commit_seq=seq, timestamp=at,
                )
            for sid, rev, values in staged:
                _patch(
                    conn, "layout_slots", sid,
                    expected_revision=rev, attrs=values,
                    commit_seq=seq, timestamp=at,
                )
            for pid, rev, target_slot in staged_bindings:
                _patch(
                    conn, "panels", pid,
                    expected_revision=rev, attrs={"layout_slot_id": target_slot},
                    commit_seq=seq, timestamp=at,
                )
            return layout


class PanelRepository:
    """Panel semantics are independent of Layout geometry and Slot identity."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def create_panel(
        self,
        *,
        panel_id: str,
        page_id: str,
        order_index: int,
        panel_purpose: str,
        panel_role: str = "STANDARD",
        layout_slot_id: str | None = None,
        action: dict[str, Any] | None = None,
        camera: dict[str, Any] | None = None,
        emotion_requirements: dict[str, Any] | None = None,
        environment_requirements: dict[str, Any] | None = None,
        continuity_requirements: dict[str, Any] | None = None,
        generation_spec: dict[str, Any] | None = None,
        status: str = "PLANNED",
    ) -> dict[str, Any]:
        attrs = _values({
            "order_index": order_index,
            "panel_purpose": panel_purpose,
            "panel_role": panel_role,
            "layout_slot_id": layout_slot_id,
            "action_json": {} if action is None else action,
            "camera_json": {} if camera is None else camera,
            "emotion_requirements_json": (
                {} if emotion_requirements is None else emotion_requirements
            ),
            "environment_requirements_json": (
                {} if environment_requirements is None else environment_requirements
            ),
            "continuity_requirements_json": (
                {} if continuity_requirements is None else continuity_requirements
            ),
            "generation_spec_json": {} if generation_spec is None else generation_spec,
            "status": status,
        }, _PANEL_FIELDS)
        with repository_write(self.database_path) as conn:
            _check_page(conn, _id(page_id, "page_id"))
            if layout_slot_id is not None:
                _check_slot_page(conn, _id(layout_slot_id, "layout_slot_id"), page_id)
            seq, at = _commit(conn, "create_panel")
            return _insert(
                conn, "panels", _id(panel_id, "panel_id"),
                {"page_id": page_id, **attrs},
                commit_seq=seq, timestamp=at,
            )

    def get_panel(self, panel_id: str) -> dict[str, Any]:
        with repository_read(self.database_path) as conn:
            return _read_row(conn, "panels", _id(panel_id, "panel_id"))

    def list_panels(self, page_id: str) -> list[dict[str, Any]]:
        with repository_read(self.database_path) as conn:
            _check_page(conn, _id(page_id, "page_id"))
            return [
                dict(row) for row in conn.execute(
                    "SELECT * FROM panels WHERE page_id = ? ORDER BY order_index, id",
                    (page_id,),
                )
            ]

    def update_panel(
        self, panel_id: str, *, expected_revision: int, **changes: Any
    ) -> dict[str, Any]:
        attrs = _values(changes, _PANEL_FIELDS)
        with repository_write(self.database_path) as conn:
            existing = _read_row(conn, "panels", _id(panel_id, "panel_id"))
            # Preserve 409-style optimistic concurrency semantics even when
            # the caller also supplies an invalid candidate. Both checks and
            # the actual Panel write share this BEGIN IMMEDIATE transaction.
            assert_expected_revision(
                entity_type="panels", entity_id=panel_id,
                expected_revision=expected_revision,
                actual_revision=existing["revision"],
            )
            if "selected_candidate_id" in attrs:
                _validate_selected_candidate(
                    conn, panel_id, attrs["selected_candidate_id"]
                )
            if attrs.get("layout_slot_id") is not None:
                _check_slot_page(
                    conn, _id(attrs["layout_slot_id"], "layout_slot_id"),
                    existing["page_id"],
                )
            record, _ = _patch(
                conn, "panels", panel_id,
                expected_revision=expected_revision, attrs=attrs,
            )
            return record


__all__ = [
    "LayoutRepository",
    "PageDomainArchivedError",
    "PageDomainCandidateSelectionError",
    "PageDomainNotFoundError",
    "PageDomainOwnershipError",
    "PageRepository",
    "PanelRepository",
]
