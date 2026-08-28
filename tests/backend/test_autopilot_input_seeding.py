"""The project's persisted settings must reach the run that consumes them.

``docs/anima_mvp.md`` tells the user to select the profile and acknowledge the
licence with ``PATCH /projects/{id}``. Both land on the project. But strict
mode (``_is_anima_run``), the preflight and the licence check all read
``run.input``, which was built from the start request body alone - nothing ever
copied the project's settings across.

The consequence was quiet and wrong: a project configured exactly as documented
and started with no body got the Anima review gates, because the review
coordinator *does* read the project, while strict mode stayed off and preflight
never ran at all. The documented promise "Preflight refuses to generate until
this is set" did not hold.
"""

from __future__ import annotations

from pathlib import Path

from manga_autopilot.routes.autopilot_routes import _seed_input_from_project
from manga_autopilot.services.project_manager import ProjectManager


def _project(tmp_path: Path, **fields):
    manager = ProjectManager(tmp_path)
    project = manager.create(name="seeding", project_id="proj-seed")
    for key, value in fields.items():
        setattr(project, key, value)
    return manager.save(project)


def test_profile_and_licence_come_from_the_project(tmp_path: Path) -> None:
    _project(tmp_path, generation_profile_id="anima_turbo", license_acknowledged=True)

    seeded = _seed_input_from_project(tmp_path, "proj-seed", {})

    assert seeded["generation_profile_id"] == "anima_turbo"
    assert seeded["license_acknowledged"] is True


def test_the_request_body_wins_over_the_project(tmp_path: Path) -> None:
    _project(tmp_path, generation_profile_id="anima_turbo", license_acknowledged=True)

    seeded = _seed_input_from_project(
        tmp_path,
        "proj-seed",
        {"generation_profile_id": "anima_base", "license_acknowledged": False},
    )

    assert seeded["generation_profile_id"] == "anima_base"
    assert seeded["license_acknowledged"] is False


def test_an_unacknowledged_licence_is_not_seeded(tmp_path: Path) -> None:
    """Only a real acknowledgement travels; absence must stay absent."""

    _project(tmp_path, generation_profile_id="anima_turbo", license_acknowledged=False)

    seeded = _seed_input_from_project(tmp_path, "proj-seed", {})

    assert "license_acknowledged" not in seeded


def test_other_input_keys_survive(tmp_path: Path) -> None:
    _project(tmp_path, generation_profile_id="anima_turbo")

    seeded = _seed_input_from_project(tmp_path, "proj-seed", {"page_count": 2})

    assert seeded["page_count"] == 2
    assert seeded["generation_profile_id"] == "anima_turbo"


def test_a_missing_project_returns_the_payload_unchanged(tmp_path: Path) -> None:
    seeded = _seed_input_from_project(tmp_path, "no-such-project", {"idea": "x"})

    assert seeded == {"idea": "x"}


def test_an_empty_profile_is_not_seeded(tmp_path: Path) -> None:
    """A generic project must not gain a ``generation_profile_id`` key."""

    _project(tmp_path)

    seeded = _seed_input_from_project(tmp_path, "proj-seed", {})

    assert seeded == {}
