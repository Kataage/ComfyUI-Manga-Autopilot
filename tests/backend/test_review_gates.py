"""Review gates for strict Anima projects (plan Task 7, steps 1-4).

Legacy projects carry no gates and run straight through. A strict Anima project
pauses after Story and after Storyboard, cannot start image generation before
Storyboard approval, and reviews artwork twice: once after the first page and
once before lettering.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from manga_autopilot.models.page import PanelPlan
from manga_autopilot.models.panel import PanelRecord
from manga_autopilot.services.review_gate import (
    ARTWORK_EARLY,
    ARTWORK_FINAL,
    REVIEW_GATES,
    STORY,
    STORYBOARD,
    ReviewCoordinator,
    ReviewNotApprovedError,
    ReviewPolicy,
    ReviewRejectedError,
    ReviewStore,
    split_for_early_review,
    unknown_gate_error,
)


def _record(page_number: int, panel_number: int) -> PanelRecord:
    return PanelRecord(
        panel_id=f"p{page_number}_{panel_number:02d}",
        page_number=page_number,
        plan=PanelPlan(panel_number=panel_number, purpose="beat", shot="medium"),
    )


def _board(tmp_path: Path, profile_id: str = "anima_turbo"):
    return ReviewStore(tmp_path).load("proj-1", ReviewPolicy.for_profile(profile_id))


# ------------------------------------------------------------------ policy


def test_anima_projects_get_every_gate() -> None:
    policy = ReviewPolicy.for_profile("anima_turbo")

    assert policy.gates == list(REVIEW_GATES)
    assert policy.is_enabled(STORYBOARD) is True


def test_legacy_projects_get_no_gates() -> None:
    for profile_id in ("", None, "generic_sdxl"):
        policy = ReviewPolicy.for_profile(profile_id)
        assert policy.gates == []
        assert policy.is_enabled(STORYBOARD) is False


def test_legacy_projects_never_block() -> None:
    board = ReviewStore(Path(".")).load("proj-legacy", ReviewPolicy.for_profile(None))

    board.require(STORYBOARD)  # must not raise
    assert board.is_approved(STORYBOARD) is True


# ------------------------------------------------------------------- board


def test_a_new_anima_board_blocks_every_gate(tmp_path: Path) -> None:
    board = _board(tmp_path)

    for gate in REVIEW_GATES:
        assert board.is_approved(gate) is False
        with pytest.raises(ReviewNotApprovedError, match=gate):
            board.require(gate)


def test_generation_cannot_start_before_storyboard_approval(tmp_path: Path) -> None:
    board = _board(tmp_path)
    board.approve(STORY)

    with pytest.raises(ReviewNotApprovedError, match=STORYBOARD):
        board.require(STORYBOARD)

    board.approve(STORYBOARD)
    board.require(STORYBOARD)


def test_approval_is_idempotent(tmp_path: Path) -> None:
    board = _board(tmp_path)

    first = board.approve(STORY, note="looks good")
    second = board.approve(STORY, note="looks good")

    assert first.status == second.status == "approved"
    assert len(board.gates[STORY].decisions) == 1


def test_a_changed_decision_is_recorded(tmp_path: Path) -> None:
    board = _board(tmp_path)

    board.approve(STORY)
    board.reject(STORY, note="page 2 makes no sense")

    assert board.gates[STORY].status == "rejected"
    assert [d.decision for d in board.gates[STORY].decisions] == ["approved", "rejected"]
    assert board.is_approved(STORY) is False


def test_an_unknown_gate_is_rejected(tmp_path: Path) -> None:
    board = _board(tmp_path)

    with pytest.raises(KeyError, match="unknown review gate"):
        board.approve("colouring")
    assert "colouring" in str(unknown_gate_error("colouring"))


# ------------------------------------------------------------- persistence


def test_board_round_trips_through_reviews_json(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path)
    board = store.load("proj-1", ReviewPolicy.for_profile("anima_turbo"))
    board.approve(STORY, note="ship it")
    path = store.save(board)

    assert path == tmp_path / "reviews.json"
    restored = store.load("proj-1", ReviewPolicy.for_profile("anima_turbo"))
    assert restored.is_approved(STORY) is True
    assert restored.gates[STORY].decisions[0].note == "ship it"
    assert restored.is_approved(STORYBOARD) is False


def test_saving_leaves_no_temporary_file(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path)
    store.save(store.load("proj-1", ReviewPolicy.for_profile("anima_turbo")))

    assert [p.name for p in tmp_path.iterdir()] == ["reviews.json"]


def test_a_stored_board_keeps_its_policy(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path)
    store.save(store.load("proj-1", ReviewPolicy.for_profile("anima_turbo")))

    document = json.loads((tmp_path / "reviews.json").read_text(encoding="utf-8"))

    assert document["policy"]["gates"] == list(REVIEW_GATES)


# ------------------------------------------------------------- coordinator


async def test_coordinator_returns_immediately_for_a_disabled_gate(tmp_path: Path) -> None:
    board = ReviewStore(tmp_path).load("proj-legacy", ReviewPolicy.for_profile(None))
    coordinator = ReviewCoordinator(board=board)

    state = await asyncio.wait_for(coordinator.wait_for(STORY), timeout=1)

    assert state.status == "approved"


async def test_coordinator_returns_immediately_for_an_approved_gate(tmp_path: Path) -> None:
    board = _board(tmp_path)
    board.approve(STORY)
    coordinator = ReviewCoordinator(board=board)

    state = await asyncio.wait_for(coordinator.wait_for(STORY), timeout=1)

    assert state.status == "approved"


async def test_coordinator_blocks_until_approved(tmp_path: Path) -> None:
    board = _board(tmp_path)
    coordinator = ReviewCoordinator(board=board)
    waiting: list[str] = []

    async def _approve_soon() -> None:
        await asyncio.sleep(0)
        coordinator.approve(STORYBOARD)

    task = asyncio.create_task(
        coordinator.wait_for(STORYBOARD, on_wait=lambda gate: waiting.append(gate))
    )
    await _approve_soon()
    state = await asyncio.wait_for(task, timeout=1)

    assert waiting == [STORYBOARD]
    assert state.status == "approved"


async def test_coordinator_raises_when_a_gate_is_rejected(tmp_path: Path) -> None:
    board = _board(tmp_path)
    coordinator = ReviewCoordinator(board=board)

    async def _reject_soon() -> None:
        await asyncio.sleep(0)
        coordinator.reject(STORYBOARD, note="layout is unreadable")

    task = asyncio.create_task(coordinator.wait_for(STORYBOARD))
    await _reject_soon()

    with pytest.raises(ReviewRejectedError, match="unreadable"):
        await asyncio.wait_for(task, timeout=1)


async def test_waiting_marks_the_gate_awaiting_review_and_persists_it(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path)
    board = store.load("proj-1", ReviewPolicy.for_profile("anima_turbo"))
    coordinator = ReviewCoordinator(board=board, store=store)

    task = asyncio.create_task(coordinator.wait_for(STORY))
    await asyncio.sleep(0)

    document = json.loads((tmp_path / "reviews.json").read_text(encoding="utf-8"))
    assert document["gates"][STORY]["status"] == "awaiting_review"

    coordinator.approve(STORY)
    await asyncio.wait_for(task, timeout=1)
    assert json.loads((tmp_path / "reviews.json").read_text(encoding="utf-8"))["gates"][STORY][
        "status"
    ] == "approved"


async def test_approving_a_gate_nobody_waits_on_is_still_recorded(tmp_path: Path) -> None:
    board = _board(tmp_path)
    coordinator = ReviewCoordinator(board=board)

    coordinator.approve(ARTWORK_FINAL)

    assert board.is_approved(ARTWORK_FINAL) is True
    state = await asyncio.wait_for(coordinator.wait_for(ARTWORK_FINAL), timeout=1)
    assert state.status == "approved"


# --------------------------------------------------------- artwork cadence


def test_early_review_splits_off_the_first_page() -> None:
    records = [_record(1, 1), _record(1, 2), _record(2, 1), _record(3, 1)]

    first, remainder = split_for_early_review(records)

    assert [r.panel_id for r in first] == ["p1_01", "p1_02"]
    assert [r.panel_id for r in remainder] == ["p2_01", "p3_01"]


def test_a_single_page_project_has_no_remainder() -> None:
    records = [_record(1, 1), _record(1, 2)]

    first, remainder = split_for_early_review(records)

    assert len(first) == 2
    assert remainder == []


def test_split_uses_the_lowest_page_number_not_list_order() -> None:
    records = [_record(3, 1), _record(1, 1), _record(2, 1)]

    first, remainder = split_for_early_review(records)

    assert [r.panel_id for r in first] == ["p1_01"]
    assert [r.page_number for r in remainder] == [3, 2]


def test_splitting_nothing_yields_nothing() -> None:
    assert split_for_early_review([]) == ([], [])


def test_artwork_gates_are_distinct_and_ordered() -> None:
    assert REVIEW_GATES.index(ARTWORK_EARLY) < REVIEW_GATES.index(ARTWORK_FINAL)
    assert REVIEW_GATES.index(STORYBOARD) < REVIEW_GATES.index(ARTWORK_EARLY)
    assert REVIEW_GATES.index(STORY) == 0


# ------------------------------------------------- early-review orchestration


class _Result:
    def __init__(self, panel_id: str, status: str) -> None:
        self.panel_id = panel_id
        self.status = status


async def test_early_review_generates_page_one_then_waits_then_the_rest(
    tmp_path: Path,
) -> None:
    from manga_autopilot.services.review_gate import run_with_early_artwork_review

    records = [_record(1, 1), _record(2, 1), _record(3, 1)]
    coordinator = ReviewCoordinator(board=_board(tmp_path))
    batches: list[list[str]] = []

    async def _generate(batch):
        batches.append([r.panel_id for r in batch])
        return [_Result(r.panel_id, "generated") for r in batch]

    task = asyncio.create_task(
        run_with_early_artwork_review(
            records, generate=_generate, coordinator=coordinator
        )
    )
    await asyncio.sleep(0)

    assert batches == [["p1_01"]], "the rest must wait for the early review"

    coordinator.approve(ARTWORK_EARLY)
    results = await asyncio.wait_for(task, timeout=1)

    assert batches == [["p1_01"], ["p2_01", "p3_01"]]
    assert [r.panel_id for r in results] == ["p1_01", "p2_01", "p3_01"]


async def test_early_review_is_skipped_when_page_one_did_not_complete(
    tmp_path: Path,
) -> None:
    from manga_autopilot.services.review_gate import run_with_early_artwork_review

    records = [_record(1, 1), _record(2, 1)]
    coordinator = ReviewCoordinator(board=_board(tmp_path))
    batches: list[list[str]] = []

    async def _generate(batch):
        batches.append([r.panel_id for r in batch])
        return [_Result(r.panel_id, "rejected") for r in batch]

    results = await asyncio.wait_for(
        run_with_early_artwork_review(records, generate=_generate, coordinator=coordinator),
        timeout=1,
    )

    assert batches == [["p1_01"]]
    assert [r.status for r in results] == ["rejected"]


async def test_a_single_page_project_skips_the_early_review(tmp_path: Path) -> None:
    from manga_autopilot.services.review_gate import run_with_early_artwork_review

    coordinator = ReviewCoordinator(board=_board(tmp_path))

    async def _generate(batch):
        return [_Result(r.panel_id, "generated") for r in batch]

    results = await asyncio.wait_for(
        run_with_early_artwork_review(
            [_record(1, 1), _record(1, 2)], generate=_generate, coordinator=coordinator
        ),
        timeout=1,
    )

    assert len(results) == 2
    assert coordinator.board.gates[ARTWORK_EARLY].status == "pending"
