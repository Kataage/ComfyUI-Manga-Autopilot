"""Project autosave loop (spec section 27.1)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AutosaveResult:
    saved_at: float
    bytes_written: int
    path: Path


AutosaveWriter = Callable[[], Awaitable[dict[str, Any]]]


class ProjectAutosave:
    """Background task that periodically persists a project snapshot.

    The actual serialisation is delegated to a user-supplied async writer
    (typically a project manager call).  The loop cancels cleanly on
    :meth:`stop` or :meth:`close`, and is a no-op while the manager is
    disabled.
    """

    def __init__(
        self,
        target_path: Path,
        writer: AutosaveWriter,
        *,
        interval_sec: float = 30.0,
        enabled: bool = True,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError("interval_sec must be positive")
        self._target_path = Path(target_path)
        self._writer = writer
        self._interval = interval_sec
        self._enabled = enabled
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_saved: AutosaveResult | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False
        # If we are mid-loop, request cancellation; the next tick will exit.
        if self._task is not None and not self._task.done():
            self._stopping.set()

    @property
    def last_result(self) -> AutosaveResult | None:
        return self._last_saved

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="manga-autosave")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._interval * 2)
            except asyncio.TimeoutError:
                self._task.cancel()
            finally:
                self._task = None

    async def close(self) -> None:
        """Same as :meth:`stop` — provided for parity with async context use."""

        await self.stop()

    # ------------------------------------------------------------ core loop
    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            if self._stopping.is_set() or not self._enabled:
                return
            try:
                await self._save_once()
            except Exception as exc:  # noqa: BLE001 - we want to keep the loop alive
                log.exception("autosave iteration failed: %s", exc)

    async def _save_once(self) -> AutosaveResult:
        snapshot = await self._writer()
        self._target_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        data = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
        self._target_path.write_text(data, encoding="utf-8")
        self._last_saved = AutosaveResult(
            saved_at=time.time(),
            bytes_written=len(data.encode("utf-8")),
            path=self._target_path,
        )
        return self._last_saved

    async def save_now(self) -> AutosaveResult:
        """Force an immediate save (used by the UI's "save" button)."""

        return await self._save_once()


__all__ = ["AutosaveResult", "ProjectAutosave", "AutosaveWriter"]
