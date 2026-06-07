"""HTTP routes for the autopilot (spec section 21.3)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from manga_autopilot.services.autopilot import (
    AutopilotController,
    AutopilotStateMachine,
    InvalidTransitionError,
)

if TYPE_CHECKING:
    from aiohttp.web import Application

log = logging.getLogger(__name__)

ROUTE_PREFIX = "/manga_autopilot/api/projects/{project_id}/autopilot"


def _controller(app: Application) -> AutopilotController:
    ctrl: Any = app.get("manga_autopilot_controller")
    if ctrl is None:
        ctrl = AutopilotController()
        app["manga_autopilot_controller"] = ctrl
    return ctrl


async def start(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    machine = AutopilotStateMachine(project_id=project_id)
    try:
        run = ctrl.start(project_id, machine)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    return web.json_response(run.to_status(), status=202)


async def pause(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        run = ctrl.pause(project_id)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(run.to_status())


async def resume(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        run = ctrl.resume(project_id)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(run.to_status())


async def cancel(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        run = ctrl.cancel(project_id)
    except InvalidTransitionError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(run.to_status())


async def status(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    ctrl = _controller(request.app)
    try:
        snapshot = ctrl.status(project_id)
    except KeyError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(snapshot)


def register(router: Any) -> None:
    if hasattr(router, "router"):
        router = router.router
    router.add_post(ROUTE_PREFIX + "/start", start)
    router.add_post(ROUTE_PREFIX + "/pause", pause)
    router.add_post(ROUTE_PREFIX + "/resume", resume)
    router.add_post(ROUTE_PREFIX + "/cancel", cancel)
    router.add_get(ROUTE_PREFIX + "/status", status)


__all__ = ["register", "ROUTE_PREFIX"]
