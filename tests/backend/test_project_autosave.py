"""Tests for the project autosave loop."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from manga_autopilot.services.project_autosave import ProjectAutosave


async def test_autosave_writes_to_disk(tmp_path: Path) -> None:
    target = tmp_path / "project.json"
    counter = {"n": 0}

    async def writer() -> dict:
        counter["n"] += 1
        return {"n": counter["n"], "ts": time.time()}

    autosave = ProjectAutosave(target, writer, interval_sec=0.1)
    await autosave.start()
    try:
        # Two intervals should produce >= 2 writes (timing-tolerant).
        await asyncio.sleep(0.25)
    finally:
        await autosave.stop()

    payload = json.loads(target.read_text("utf-8"))
    assert payload["n"] >= 1
    assert autosave.last_result is not None
    assert autosave.last_result.path == target


async def test_autosave_can_be_disabled(tmp_path: Path) -> None:
    target = tmp_path / "project.json"
    counter = {"n": 0}

    async def writer() -> dict:
        counter["n"] += 1
        return {"n": counter["n"]}

    autosave = ProjectAutosave(target, writer, interval_sec=0.05, enabled=False)
    await autosave.start()
    await asyncio.sleep(0.15)
    await autosave.stop()
    assert counter["n"] == 0
    assert not target.exists()


async def test_autosave_save_now(tmp_path: Path) -> None:
    target = tmp_path / "project.json"

    async def writer() -> dict:
        return {"hello": "world"}

    autosave = ProjectAutosave(target, writer, interval_sec=10.0)
    result = await autosave.save_now()
    assert result.bytes_written > 0
    payload = json.loads(target.read_text("utf-8"))
    assert payload == {"hello": "world"}


def test_autosave_rejects_non_positive_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ProjectAutosave(tmp_path / "x.json", lambda: asyncio.sleep(0), interval_sec=0)


async def test_autosave_writer_exception_keeps_loop_alive(tmp_path: Path) -> None:
    target = tmp_path / "project.json"
    counter = {"n": 0}

    async def writer() -> dict:
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("transient")
        return {"n": counter["n"]}

    autosave = ProjectAutosave(target, writer, interval_sec=0.05)
    await autosave.start()
    try:
        await asyncio.sleep(0.2)
    finally:
        await autosave.stop()

    # The loop kept going after the failure and wrote at least one snapshot.
    assert counter["n"] >= 2
    payload = json.loads(target.read_text("utf-8"))
    assert payload["n"] >= 2


async def test_autosave_start_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "project.json"

    async def writer() -> dict:
        return {}

    autosave = ProjectAutosave(target, writer, interval_sec=10.0)
    await autosave.start()
    first = autosave._task
    await autosave.start()  # no-op
    assert autosave._task is first
    await autosave.stop()
