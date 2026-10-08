"""Subprocess fixture for real Python-process durable Autopilot recovery E2E.

Invoked only by test_durable_process_restart_e2e.py. Each invocation creates
a fresh aiohttp Application, repository, orchestrator and in-memory Run.
Do not import or execute this module in the test runner process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from manga_autopilot.repositories.durable_runs import DurableRunRepository
from manga_autopilot.services.autopilot import OrchestratorHooks
from manga_autopilot.services.durable_autopilot import (
    DurableAutopilotOrchestrator,
    RetryableStepError,
)

_FIXED_NOW = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
_CRASH_EXIT = 83


def _record(path: Path, *, stage: str, app_id: str, **values: Any) -> None:
    record = {"stage": stage, "app_id": app_id, **values}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _build_application(args: argparse.Namespace) -> web.Application:
    """Construct a genuinely new application instance over a persisted DB."""
    app = web.Application()
    app_id = secrets.token_hex(12)
    clock = lambda: _FIXED_NOW + timedelta(seconds=args.clock_offset)
    repository = DurableRunRepository(args.database, clock=clock)

    async def validate_input(run: Any) -> dict[str, str]:
        _record(args.events, stage="validate_input", app_id=app_id)
        return {"title": str(run.input["title"])}

    async def plan_story(run: Any) -> dict[str, str]:
        assert run.artefacts["validate_input"] == {"title": "Work #247"}
        _record(args.events, stage="plan_story", app_id=app_id)
        return {"story": "persisted-scene"}

    async def generate_panels(run: Any) -> dict[str, str]:
        assert run.artefacts["plan_story"] == {"story": "persisted-scene"}
        _record(args.events, stage="generate_panels", app_id=app_id,
                generation_version=args.generation_version)
        if args.mode == "crash_generation":
            # Crash between the persisted RUNNING receipt and its completion.
            # Finally blocks, destructors and SQLite cleanup do not run.
            os._exit(_CRASH_EXIT)
        return {"candidate": args.generation_version}

    def render_pages(run: Any) -> dict[str, str]:
        assert run.artefacts["generate_panels"] == {
            "candidate": args.generation_version,
        }
        _record(args.events, stage="render_pages", app_id=app_id,
                candidate=run.artefacts["generate_panels"]["candidate"])
        if args.mode == "retryable_render_failure":
            raise RetryableStepError("controlled render dependency interruption")
        return {"page": "page-001.png"}

    def export(run: Any) -> dict[str, str]:
        assert run.artefacts["render_pages"] == {"page": "page-001.png"}
        _record(args.events, stage="export", app_id=app_id)
        return {"archive": "work-001.cbz"}

    hooks = OrchestratorHooks(
        validate_input=validate_input,
        plan_story=plan_story,
        generate_panels=generate_panels,
        render_pages=render_pages,
        export=export,
    )
    app["worker_identity"] = app_id
    app["durable_repository"] = repository
    app["durable_orchestrator"] = DurableAutopilotOrchestrator(
        repository=repository, work_id="work_247", hooks=hooks,
        lease_ttl_seconds=10,
    )
    return app


async def _execute(args: argparse.Namespace) -> dict[str, Any]:
    app = _build_application(args)
    orchestrator = app["durable_orchestrator"]
    try:
        result = await orchestrator.execute(
            args.run_id,
            input_payload={"title": "Work #247"},
            step_inputs={"generate_panels": args.generation_version},
            lease_owner=args.owner,
            reclaim_expired_owner=args.reclaim_owner,
            approve_interrupted_retry=args.approve_interrupted,
        )
    except Exception as exc:  # noqa: BLE001 - report errors to parent pytest
        return {
            "outcome": "rejected",
            "type": type(exc).__name__,
            "message": str(exc),
            "app_id": app["worker_identity"],
        }
    return {
        "outcome": "finished",
        "state": result.machine.state.value,
        "durable_status": app["durable_repository"].get_run(
            args.run_id,
        )["status"],
        "app_id": app["worker_identity"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=(
        "crash_generation", "retryable_render_failure", "normal",
    ), required=True)
    parser.add_argument("--generation-version", default="v1")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--reclaim-owner")
    parser.add_argument("--approve-interrupted", action="store_true")
    parser.add_argument("--clock-offset", type=int, default=0)
    args = parser.parse_args()
    summary = asyncio.run(_execute(args))
    print(json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
