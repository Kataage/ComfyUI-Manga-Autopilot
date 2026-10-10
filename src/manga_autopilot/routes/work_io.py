"""Own synchronous Work/SQLite operations without blocking the aiohttp loop.

Worker operations never receive aiohttp Request/Response instances. A cancelled
HTTP coroutine drains its owned worker even after repeated cancellation, so an
in-flight SQLite write cannot detach and commit behind an abandoned handler.
A commit already completed before cancellation cannot be rolled back: clients
must re-read persisted revisions rather than assuming a cancelled PATCH undid it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def run_owned_work_io(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one blocking storage call in a worker; retain ownership on cancel."""
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # to_thread cancellation only cancels its asyncio waiter. The OS worker
        # must complete its SQLite transaction/rollback before handler exit.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            try:
                worker.result()
            except BaseException:
                pass  # Original caller cancellation remains authoritative.
        raise


__all__ = ["run_owned_work_io"]
