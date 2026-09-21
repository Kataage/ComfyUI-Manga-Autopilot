"""Review gates for strict Anima projects.

A strict run stops and asks four times: after the story is planned, after the
storyboard is laid out (nothing may reach the GPU before this one), after the
first page of artwork exists, and once more before lettering.

Decisions live in ``reviews.json`` so an approval survives a restart, and the
coordinator waits on its own per-gate event rather than the user's pause event -
a run waiting for a review is not the same thing as a user who pressed pause,
and conflating the two makes "resume" mean two different things.

Legacy and generic projects carry an empty policy, so every gate reports
approved and nothing blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from manga_autopilot.models.panel import PanelRecord

log = logging.getLogger(__name__)

STORY = "story"
STORYBOARD = "storyboard"
ARTWORK_EARLY = "artwork_early"
ARTWORK_FINAL = "artwork_final"

#: Gates in the order a run encounters them.
REVIEW_GATES: tuple[str, ...] = (STORY, STORYBOARD, ARTWORK_EARLY, ARTWORK_FINAL)

REVIEWS_FILENAME = "reviews.json"


class ReviewNotApprovedError(RuntimeError):
    """Raised when work is attempted before its gate was approved."""


class ReviewRejectedError(RuntimeError):
    """Raised when a waiting run's gate is rejected."""


def unknown_gate_error(gate: str) -> KeyError:
    return KeyError(f"unknown review gate: {gate!r}; expected one of {list(REVIEW_GATES)}")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewPolicy(BaseModel):
    """Which gates a project uses. An empty list means the project never pauses."""

    model_config = ConfigDict(extra="ignore")

    gates: list[str] = Field(default_factory=list)

    @classmethod
    def for_profile(cls, generation_profile_id: str | None) -> ReviewPolicy:
        """Anima profiles review everything; anything else reviews nothing."""
        profile_id = generation_profile_id or ""
        if profile_id.startswith("anima_"):
            return cls(gates=list(REVIEW_GATES))
        return cls(gates=[])

    def is_enabled(self, gate: str) -> bool:
        return gate in self.gates


class ReviewDecision(BaseModel):
    """One human decision on one gate."""

    model_config = ConfigDict(extra="ignore")

    decision: str
    at: str = Field(default_factory=_utc_now_iso)
    note: str = Field(default="", max_length=2048)
    by: str = Field(default="", max_length=128)


class ReviewGateState(BaseModel):
    """Current status of one gate plus its decision history."""

    model_config = ConfigDict(extra="ignore")

    gate: str
    status: str = "pending"
    decisions: list[ReviewDecision] = Field(default_factory=list)
    updated_at: str = Field(default_factory=_utc_now_iso)


class ReviewBoard(BaseModel):
    """Every gate for one project."""

    model_config = ConfigDict(extra="ignore")

    project_id: str
    policy: ReviewPolicy = Field(default_factory=ReviewPolicy)
    gates: dict[str, ReviewGateState] = Field(default_factory=dict)

    def ensure_gates(self) -> ReviewBoard:
        for gate in self.policy.gates:
            self.gates.setdefault(gate, ReviewGateState(gate=gate))
        return self

    def _state(self, gate: str) -> ReviewGateState:
        if gate not in REVIEW_GATES:
            raise unknown_gate_error(gate)
        return self.gates.setdefault(gate, ReviewGateState(gate=gate))

    def is_approved(self, gate: str) -> bool:
        """Whether work behind `gate` may proceed. Disabled gates are always open."""
        if not self.policy.is_enabled(gate):
            return True
        return self._state(gate).status == "approved"

    def require(self, gate: str) -> None:
        """Raise unless `gate` is approved (or not part of this project's policy).

        Raises:
            ReviewNotApprovedError: the gate is enabled and not yet approved.
        """
        if self.is_approved(gate):
            return
        state = self._state(gate)
        raise ReviewNotApprovedError(
            f"review gate {gate!r} is {state.status}; it must be approved first"
        )

    def approve(self, gate: str, *, note: str = "", by: str = "") -> ReviewGateState:
        return self._decide(gate, "approved", note=note, by=by)

    def reject(self, gate: str, *, note: str = "", by: str = "") -> ReviewGateState:
        return self._decide(gate, "rejected", note=note, by=by)

    def mark_awaiting(self, gate: str) -> ReviewGateState:
        state = self._state(gate)
        if state.status in {"approved", "rejected"}:
            return state
        state.status = "awaiting_review"
        state.updated_at = _utc_now_iso()
        return state

    def _decide(self, gate: str, decision: str, *, note: str, by: str) -> ReviewGateState:
        """Apply a decision. Repeating the standing decision changes nothing."""
        state = self._state(gate)
        if state.status == decision:
            return state
        state.decisions.append(ReviewDecision(decision=decision, note=note, by=by))
        state.status = decision
        state.updated_at = _utc_now_iso()
        log.info("review gate %s -> %s", gate, decision)
        return state


@dataclass
class ReviewStore:
    """Read and write ``reviews.json`` under a project root."""

    project_root: Path

    @property
    def path(self) -> Path:
        return Path(self.project_root) / REVIEWS_FILENAME

    def load(self, project_id: str, policy: ReviewPolicy) -> ReviewBoard:
        """Load the stored board, or build a fresh one for `policy`."""
        if self.path.exists():
            document = json.loads(self.path.read_text(encoding="utf-8"))
            board = ReviewBoard.model_validate(document)
            board.policy = policy
            return board.ensure_gates()
        return ReviewBoard(project_id=project_id, policy=policy).ensure_gates()

    def save(self, board: ReviewBoard) -> Path:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(
                json.dumps(board.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(target)
        finally:
            if temp.exists():
                temp.unlink()
        return target


@dataclass
class ReviewCoordinator:
    """Let a running pipeline wait for a gate without touching the pause event."""

    board: ReviewBoard
    store: ReviewStore | None = None
    _events: dict[str, asyncio.Event] = field(default_factory=dict, init=False)

    def event_for(self, gate: str) -> asyncio.Event:
        if gate not in REVIEW_GATES:
            raise unknown_gate_error(gate)
        return self._events.setdefault(gate, asyncio.Event())

    async def wait_for(
        self,
        gate: str,
        *,
        on_wait: Callable[[str], None] | None = None,
    ) -> ReviewGateState:
        """Block until `gate` is approved.

        Returns immediately when the gate is disabled or already approved.

        Raises:
            ReviewRejectedError: the gate was rejected while waiting.
        """
        if self.board.is_approved(gate):
            return self.board.gates.get(gate) or ReviewGateState(gate=gate, status="approved")

        self.board.mark_awaiting(gate)
        self._persist()
        log.info("waiting for review gate %s", gate)
        if on_wait is not None:
            on_wait(gate)

        await self.event_for(gate).wait()

        state = self.board.gates[gate]
        if state.status == "rejected":
            note = state.decisions[-1].note if state.decisions else ""
            raise ReviewRejectedError(f"review gate {gate!r} was rejected: {note}")
        return state

    def approve(self, gate: str, *, note: str = "", by: str = "") -> ReviewGateState:
        state = self.board.approve(gate, note=note, by=by)
        self._persist()
        self.event_for(gate).set()
        return state

    def reject(self, gate: str, *, note: str = "", by: str = "") -> ReviewGateState:
        state = self.board.reject(gate, note=note, by=by)
        self._persist()
        self.event_for(gate).set()
        return state

    def _persist(self) -> None:
        if self.store is not None:
            self.store.save(self.board)


async def run_with_early_artwork_review(
    records: Sequence[PanelRecord],
    *,
    generate: Callable[[list[PanelRecord]], object],
    coordinator: ReviewCoordinator,
    on_wait: Callable[[str], None] | None = None,
) -> list:
    """Generate the first page, pause for early artwork review, then the rest.

    The point of the pause is to spend one page of GPU time before asking the
    user whether the artwork direction is right. If the first page did not come
    out cleanly there is nothing to review, so the run stops there instead.

    `generate` receives a list of records and returns the per-panel results; it
    stays injected so this module does not depend on the generation loop.
    """
    first, remainder = split_for_early_review(records)
    if not first:
        return []

    results = list(await generate(first))
    if not remainder:
        return results
    if any(getattr(result, "status", "") != "generated" for result in results):
        log.info("skipping early artwork review: the first page did not complete")
        return results

    await coordinator.wait_for(ARTWORK_EARLY, on_wait=on_wait)
    results.extend(await generate(remainder))
    return results


def split_for_early_review(
    records: Sequence[PanelRecord],
) -> tuple[list[PanelRecord], list[PanelRecord]]:
    """Split panels into the first page and everything after it.

    The first page is generated on its own so the user can judge the artwork
    direction before the remaining pages consume GPU time. "First" means the
    lowest page number, not whatever happens to lead the list.
    """
    if not records:
        return [], []
    first_page = min(record.page_number for record in records)
    first = [record for record in records if record.page_number == first_page]
    remainder = [record for record in records if record.page_number != first_page]
    return first, remainder


__all__ = [
    "ARTWORK_EARLY",
    "ARTWORK_FINAL",
    "REVIEWS_FILENAME",
    "REVIEW_GATES",
    "STORY",
    "STORYBOARD",
    "ReviewBoard",
    "ReviewCoordinator",
    "ReviewDecision",
    "ReviewGateState",
    "ReviewNotApprovedError",
    "ReviewPolicy",
    "ReviewRejectedError",
    "ReviewStore",
    "run_with_early_artwork_review",
    "split_for_early_review",
    "unknown_gate_error",
]
