"""HTTP routes for review gates (plan Task 7, step 4).

Endpoints (all under ``/manga_autopilot/api/projects/{project_id}``):

- ``GET  /reviews``                  - the whole board
- ``POST /reviews/{gate}/approve``   - approve one gate
- ``POST /reviews/{gate}/reject``    - reject one gate

Decisions are idempotent: approving an already-approved gate returns the same
state and records nothing new. A gate a run is currently waiting on is released
through the live coordinator when one is registered for the project, so an
approval takes effect without a restart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.services.project_manager import ProjectManager, ProjectNotFoundError
from manga_autopilot.services.review_gate import (
    REVIEW_GATES,
    ReviewBoard,
    ReviewCoordinator,
    ReviewPolicy,
    ReviewStore,
)
from manga_autopilot.storage.paths import project_paths

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects/{project_id}"

#: Live coordinators, keyed by project id, for runs currently waiting on a gate.
COORDINATORS_KEY = "manga_review_coordinators"


def _storage_root(app: Application) -> Path:
    root = app.get("manga_storage_root")
    if root is None:
        raise web.HTTPInternalServerError(text="manga_storage_root is not configured")
    return Path(root)


def register_coordinator(app: Application, project_id: str, coordinator: ReviewCoordinator) -> None:
    """Publish a run's coordinator so an approval can release it immediately."""
    app.setdefault(COORDINATORS_KEY, {})[project_id] = coordinator


def unregister_coordinator(app: Application, project_id: str) -> None:
    app.get(COORDINATORS_KEY, {}).pop(project_id, None)


def _coordinator(app: Application, project_id: str) -> ReviewCoordinator | None:
    return app.get(COORDINATORS_KEY, {}).get(project_id)


def _load_board(app: Application, project_id: str) -> tuple[ReviewStore, ReviewBoard]:
    root = _storage_root(app)
    try:
        project = ProjectManager(root).load(project_id)
    except ProjectNotFoundError as exc:
        raise web.HTTPNotFound(text=f"unknown project: {project_id}") from exc
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc

    store = ReviewStore(project_paths(root, project_id).root)
    policy = ReviewPolicy.for_profile(project.generation_profile_id)
    return store, store.load(project_id, policy)


def _gate_of(request: web.Request) -> str:
    gate = request.match_info["gate"]
    if gate not in REVIEW_GATES:
        raise web.HTTPNotFound(text=f"unknown review gate: {gate}")
    return gate


async def _note(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty or malformed note is not fatal
        return {}
    return body if isinstance(body, dict) else {}


def _board_payload(board: ReviewBoard) -> dict[str, Any]:
    return {
        "project_id": board.project_id,
        "policy": board.policy.model_dump(mode="json"),
        "gates": {name: state.model_dump(mode="json") for name, state in board.gates.items()},
        "blocking_gate": next(
            (gate for gate in board.policy.gates if not board.is_approved(gate)), None
        ),
    }


async def get_reviews(request: web.Request) -> web.Response:
    _, board = _load_board(request.app, request.match_info["project_id"])
    return web.json_response(_board_payload(board))


async def _decide(request: web.Request, decision: str) -> web.Response:
    project_id = request.match_info["project_id"]
    gate = _gate_of(request)
    body = await _note(request)
    note = str(body.get("note", ""))[:2048]
    by = str(body.get("by", ""))[:128]

    live = _coordinator(request.app, project_id)
    if live is not None:
        # A run is waiting: decide through the coordinator so it wakes up.
        getattr(live, decision)(gate, note=note, by=by)
        board = live.board
        store, _ = _load_board(request.app, project_id)
        store.save(board)
    else:
        store, board = _load_board(request.app, project_id)
        getattr(board, decision)(gate, note=note, by=by)
        store.save(board)

    log.info("review %s on gate %s for project %s", decision, gate, project_id)
    return web.json_response(_board_payload(board))


async def approve_review(request: web.Request) -> web.Response:
    return await _decide(request, "approve")


async def reject_review(request: web.Request) -> web.Response:
    return await _decide(request, "reject")


def register(router: Any) -> None:
    """Register the review-gate routes on ``router``."""
    if hasattr(router, "router"):
        router = router.router
    router.add_get(ROUTE_PREFIX + "/reviews", get_reviews)
    router.add_post(ROUTE_PREFIX + "/reviews/{gate}/approve", approve_review)
    router.add_post(ROUTE_PREFIX + "/reviews/{gate}/reject", reject_review)


__all__ = [
    "COORDINATORS_KEY",
    "ROUTE_PREFIX",
    "approve_review",
    "get_reviews",
    "register",
    "register_coordinator",
    "reject_review",
    "unregister_coordinator",
]
