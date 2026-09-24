"""Schema constraint tests for v2 Page, Layout, and Panel tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from manga_autopilot.storage import (
    bootstrap_work_database,
    create_work_commit,
    repository_write,
    write_connection,
)


def _bootstrap(database: Path) -> int:
    bootstrap_work_database(database, work_id="work_001")
    with repository_write(database) as connection:
        commit = create_work_commit(
            connection,
            commit_id="commit_schema",
            actor_type="system",
            operation_type="schema_test",
            created_at="2026-09-20T00:00:00+00:00",
        )
        return commit.commit_seq


def _insert_page(
    connection: sqlite3.Connection,
    *,
    page_id: str,
    page_number: int,
    commit_seq: int,
    order_key: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO pages (
            id,
            page_number,
            order_key,
            page_role,
            page_purpose,
            narrative_goal,
            format_kind,
            status,
            revision,
            created_commit_seq,
            updated_commit_seq,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            page_id,
            page_number,
            order_key or f"{page_number:08d}",
            "STANDARD",
            f"Purpose {page_number}",
            None,
            "MANGA_PAGE",
            "PLANNED",
            1,
            commit_seq,
            commit_seq,
            "2026-09-20T00:00:00+00:00",
            "2026-09-20T00:00:00+00:00",
        ),
    )


def _insert_layout(
    connection: sqlite3.Connection,
    *,
    layout_id: str,
    page_id: str,
    commit_seq: int,
) -> None:
    connection.execute(
        """
        INSERT INTO layout_instances (
            id,
            page_id,
            source_template_id,
            template_snapshot_id,
            layout_kind,
            reading_direction,
            parameters_json,
            geometry_json,
            constraints_json,
            revision,
            created_commit_seq,
            updated_commit_seq,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            layout_id,
            page_id,
            "template_4koma",
            "TEMPLATE",
            "RTL_TOP_TO_BOTTOM",
            "{}",
            "{}",
            "{}",
            1,
            commit_seq,
            commit_seq,
            "2026-09-20T00:00:00+00:00",
            "2026-09-20T00:00:00+00:00",
        ),
    )


def _insert_slot(
    connection: sqlite3.Connection,
    *,
    slot_id: str,
    layout_id: str,
    slot_key: str,
    reading_order: int,
    commit_seq: int,
) -> None:
    connection.execute(
        """
        INSERT INTO layout_slots (
            id,
            layout_instance_id,
            slot_key,
            reading_order,
            geometry_json,
            semantic_json,
            safe_subject_region_json,
            bubble_regions_json,
            forbidden_regions_json,
            revision,
            created_commit_seq,
            updated_commit_seq,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slot_id,
            layout_id,
            slot_key,
            reading_order,
            '{"x":0,"y":0,"width":100,"height":100}',
            "{}",
            "{}",
            "[]",
            "[]",
            1,
            commit_seq,
            commit_seq,
            "2026-09-20T00:00:00+00:00",
            "2026-09-20T00:00:00+00:00",
        ),
    )


def _insert_panel(
    connection: sqlite3.Connection,
    *,
    panel_id: str,
    page_id: str,
    order_index: int,
    commit_seq: int,
    layout_slot_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO panels (
            id,
            page_id,
            layout_slot_id,
            order_index,
            panel_role,
            panel_purpose,
            entry_anchor_id,
            exit_anchor_id,
            action_json,
            camera_json,
            emotion_requirements_json,
            environment_requirements_json,
            continuity_requirements_json,
            generation_spec_json,
            selected_candidate_id,
            status,
            revision,
            created_commit_seq,
            updated_commit_seq,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            panel_id,
            page_id,
            layout_slot_id,
            order_index,
            "STANDARD",
            f"Panel purpose {order_index}",
            "{}",
            "{}",
            "{}",
            "{}",
            "{}",
            "{}",
            "PLANNED",
            1,
            commit_seq,
            commit_seq,
            "2026-09-20T00:00:00+00:00",
            "2026-09-20T00:00:00+00:00",
        ),
    )


def test_page_layout_panel_tables_exist(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    _bootstrap(database)

    with write_connection(database) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {
        "pages",
        "page_story_beats",
        "layout_instances",
        "layout_slots",
        "panels",
        "panel_story_beats",
    } <= tables


def test_page_number_is_unique(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    commit_seq = _bootstrap(database)

    with write_connection(database) as connection:
        _insert_page(
            connection,
            page_id="page_001",
            page_number=1,
            commit_seq=commit_seq,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_page(
                connection,
                page_id="page_002",
                page_number=1,
                commit_seq=commit_seq,
            )


def test_panel_requires_existing_page_and_non_null_page_id(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    commit_seq = _bootstrap(database)

    with write_connection(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_panel(
                connection,
                panel_id="panel_orphan",
                page_id="missing_page",
                order_index=1,
                commit_seq=commit_seq,
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO panels (
                    id,
                    page_id,
                    order_index,
                    panel_role,
                    panel_purpose,
                    action_json,
                    camera_json,
                    emotion_requirements_json,
                    environment_requirements_json,
                    continuity_requirements_json,
                    generation_spec_json,
                    status,
                    revision,
                    created_commit_seq,
                    updated_commit_seq,
                    created_at,
                    updated_at
                )
                VALUES (
                    'panel_null',
                    NULL,
                    1,
                    'STANDARD',
                    'Purpose',
                    '{}',
                    '{}',
                    '{}',
                    '{}',
                    '{}',
                    '{}',
                    'PLANNED',
                    1,
                    ?,
                    ?,
                    '2026-09-20T00:00:00+00:00',
                    '2026-09-20T00:00:00+00:00'
                )
                """,
                (commit_seq, commit_seq),
            )


def test_panel_order_is_unique_within_page_but_reusable_across_pages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work.sqlite3"
    commit_seq = _bootstrap(database)

    with write_connection(database) as connection:
        _insert_page(
            connection,
            page_id="page_001",
            page_number=1,
            commit_seq=commit_seq,
        )
        _insert_page(
            connection,
            page_id="page_002",
            page_number=2,
            commit_seq=commit_seq,
        )
        _insert_panel(
            connection,
            panel_id="panel_001",
            page_id="page_001",
            order_index=1,
            commit_seq=commit_seq,
        )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_panel(
                connection,
                panel_id="panel_duplicate_order",
                page_id="page_001",
                order_index=1,
                commit_seq=commit_seq,
            )

        _insert_panel(
            connection,
            panel_id="panel_page_2",
            page_id="page_002",
            order_index=1,
            commit_seq=commit_seq,
        )


def test_layout_instance_is_one_per_page(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    commit_seq = _bootstrap(database)

    with write_connection(database) as connection:
        _insert_page(
            connection,
            page_id="page_001",
            page_number=1,
            commit_seq=commit_seq,
        )
        _insert_layout(
            connection,
            layout_id="layout_001",
            page_id="page_001",
            commit_seq=commit_seq,
        )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_layout(
                connection,
                layout_id="layout_002",
                page_id="page_001",
                commit_seq=commit_seq,
            )


def test_layout_slot_reading_order_and_slot_key_are_unique_per_layout(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work.sqlite3"
    commit_seq = _bootstrap(database)

    with write_connection(database) as connection:
        _insert_page(
            connection,
            page_id="page_001",
            page_number=1,
            commit_seq=commit_seq,
        )
        _insert_layout(
            connection,
            layout_id="layout_001",
            page_id="page_001",
            commit_seq=commit_seq,
        )
        _insert_slot(
            connection,
            slot_id="slot_001",
            layout_id="layout_001",
            slot_key="top",
            reading_order=1,
            commit_seq=commit_seq,
        )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_slot(
                connection,
                slot_id="slot_duplicate_reading",
                layout_id="layout_001",
                slot_key="bottom",
                reading_order=1,
                commit_seq=commit_seq,
            )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_slot(
                connection,
                slot_id="slot_duplicate_key",
                layout_id="layout_001",
                slot_key="top",
                reading_order=2,
                commit_seq=commit_seq,
            )


def test_layout_slot_is_separate_from_panel(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite3"
    commit_seq = _bootstrap(database)

    with write_connection(database) as connection:
        _insert_page(
            connection,
            page_id="page_001",
            page_number=1,
            commit_seq=commit_seq,
        )
        _insert_layout(
            connection,
            layout_id="layout_001",
            page_id="page_001",
            commit_seq=commit_seq,
        )
        _insert_slot(
            connection,
            slot_id="slot_001",
            layout_id="layout_001",
            slot_key="main",
            reading_order=1,
            commit_seq=commit_seq,
        )
        _insert_panel(
            connection,
            panel_id="panel_001",
            page_id="page_001",
            order_index=1,
            layout_slot_id="slot_001",
            commit_seq=commit_seq,
        )

        connection.execute(
            """
            UPDATE layout_slots
            SET geometry_json = '{"x":10,"y":20,"width":80,"height":60}',
                revision = 2
            WHERE id = 'slot_001'
            """
        )

        slot = connection.execute(
            "SELECT geometry_json, revision FROM layout_slots WHERE id = 'slot_001'"
        ).fetchone()
        panel = connection.execute(
            "SELECT panel_purpose, revision, layout_slot_id FROM panels WHERE id = 'panel_001'"
        ).fetchone()

    assert slot is not None
    assert slot["revision"] == 2
    assert panel is not None
    assert panel["revision"] == 1
    assert panel["layout_slot_id"] == "slot_001"
    assert panel["panel_purpose"] == "Panel purpose 1"


def test_page_and_panel_story_beat_linkage_enforces_ownership_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "work.sqlite3"
    commit_seq = _bootstrap(database)

    with write_connection(database) as connection:
        _insert_page(
            connection,
            page_id="page_001",
            page_number=1,
            commit_seq=commit_seq,
        )
        _insert_panel(
            connection,
            panel_id="panel_001",
            page_id="page_001",
            order_index=1,
            commit_seq=commit_seq,
        )
        connection.execute(
            """
            INSERT INTO page_story_beats (
                id,
                page_id,
                sequence_key,
                story_event_id,
                beat_type,
                description,
                required,
                visual_weight,
                metadata_json,
                revision,
                created_commit_seq,
                updated_commit_seq,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "beat_001",
                "page_001",
                "001",
                "REVEAL",
                "Show the clue",
                1,
                "HIGH",
                "{}",
                1,
                commit_seq,
                commit_seq,
                "2026-09-20T00:00:00+00:00",
                "2026-09-20T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO panel_story_beats (panel_id, page_story_beat_id)
            VALUES ('panel_001', 'beat_001')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO panel_story_beats (panel_id, page_story_beat_id)
                VALUES ('panel_001', 'missing_beat')
                """
            )
