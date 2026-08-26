"""Managed LM Studio lifecycle (plan Task 6, steps 5-6).

The interfaces asserted here were read off the installed LM Studio CLI on
2026-08-26: ``lms load <model-key> [--identifier ...] [--ttl ...]``,
``lms unload <identifier>``, and ``lms ps --json`` returning objects that carry
``identifier``, ``modelKey``, and ``status``.

The session must never unload a model it did not load: at the time of writing the
user had ``gemma-4-26b-a4b-it`` loaded for their own work.
"""

from __future__ import annotations

import json
import logging

import pytest

from manga_autopilot.services.lm_studio_lifecycle import (
    LMStudioError,
    LoadedModel,
    ManagedLMStudioSession,
    redact_secrets,
)

USER_OWNED = {
    "type": "llm",
    "modelKey": "gemma-4-26b-a4b-it",
    "identifier": "gemma-4-26b-a4b-it",
    "status": "idle",
    "ttlMs": 300000,
}
PLANNER_MODEL = "qwen3.5-9b"


class FakeCli:
    """Stand-in for the ``lms`` executable. Records every argv it is handed."""

    def __init__(self, *, loaded: list[dict] | None = None, fail_on: str | None = None) -> None:
        self.loaded = list(loaded or [])
        self.commands: list[list[str]] = []
        self._fail_on = fail_on

    def __call__(self, argv: list[str]) -> str:
        self.commands.append(list(argv))
        if self._fail_on is not None and self._fail_on in argv:
            raise RuntimeError(f"lms {self._fail_on} exploded")
        command = argv[0] if argv else ""
        if command == "ps":
            return json.dumps(self.loaded)
        if command == "load":
            identifier = _option(argv, "--identifier") or argv[1]
            self.loaded.append(
                {
                    "type": "llm",
                    "modelKey": argv[1],
                    "identifier": identifier,
                    "status": "idle",
                }
            )
            return ""
        if command == "unload":
            target = argv[1]
            self.loaded = [m for m in self.loaded if m["identifier"] != target]
            return ""
        raise AssertionError(f"unexpected lms command: {argv}")


def _option(argv: list[str], name: str) -> str | None:
    if name not in argv:
        return None
    return argv[argv.index(name) + 1]


def _session(cli: FakeCli, **overrides) -> ManagedLMStudioSession:
    kwargs = {
        "run": cli,
        "model_key": PLANNER_MODEL,
        "identifier": "manga-autopilot-planner",
        "ttl_seconds": 900,
    }
    kwargs.update(overrides)
    return ManagedLMStudioSession(**kwargs)


# ----------------------------------------------------------------- listing


def test_loaded_models_are_parsed_from_ps_json() -> None:
    session = _session(FakeCli(loaded=[USER_OWNED]))

    loaded = session.list_loaded()

    assert loaded == [
        LoadedModel(identifier="gemma-4-26b-a4b-it", model_key="gemma-4-26b-a4b-it", status="idle")
    ]


def test_unparsable_ps_output_is_an_error() -> None:
    class BrokenCli(FakeCli):
        def __call__(self, argv: list[str]) -> str:
            return "not json"

    with pytest.raises(LMStudioError, match="ps"):
        _session(BrokenCli()).list_loaded()


# -------------------------------------------------------------------- load


def test_ensure_loaded_loads_the_planner_model_with_fixed_settings() -> None:
    cli = FakeCli(loaded=[USER_OWNED])
    session = _session(cli, context_length=8192)

    session.ensure_loaded()

    load = [c for c in cli.commands if c[0] == "load"][0]
    assert load[1] == PLANNER_MODEL
    assert _option(load, "--identifier") == "manga-autopilot-planner"
    assert _option(load, "--ttl") == "900"
    assert _option(load, "--context-length") == "8192"
    assert "-y" in load
    assert session.owns_instance is True
    assert session.instance_id == "manga-autopilot-planner"


def test_ensure_loaded_is_idempotent() -> None:
    cli = FakeCli()
    session = _session(cli)

    session.ensure_loaded()
    session.ensure_loaded()

    assert len([c for c in cli.commands if c[0] == "load"]) == 1


def test_an_already_loaded_planner_model_is_adopted_not_reloaded() -> None:
    existing = {
        "type": "llm",
        "modelKey": PLANNER_MODEL,
        "identifier": "someone-elses-qwen",
        "status": "idle",
    }
    cli = FakeCli(loaded=[USER_OWNED, existing])
    session = _session(cli)

    session.ensure_loaded()

    assert [c for c in cli.commands if c[0] == "load"] == []
    assert session.instance_id == "someone-elses-qwen"
    assert session.owns_instance is False


def test_load_failure_is_reported_as_an_lm_studio_error() -> None:
    session = _session(FakeCli(fail_on="load"))

    with pytest.raises(LMStudioError, match="load"):
        session.ensure_loaded()


# ------------------------------------------------------------------ unload


def test_unload_only_touches_the_instance_we_created() -> None:
    cli = FakeCli(loaded=[USER_OWNED])
    session = _session(cli)
    session.ensure_loaded()

    session.unload()

    unloads = [c for c in cli.commands if c[0] == "unload"]
    assert unloads == [["unload", "manga-autopilot-planner"]]
    assert [m["identifier"] for m in cli.loaded] == ["gemma-4-26b-a4b-it"]


def test_an_adopted_instance_is_never_unloaded() -> None:
    existing = {"type": "llm", "modelKey": PLANNER_MODEL, "identifier": "user-qwen", "status": "idle"}
    cli = FakeCli(loaded=[USER_OWNED, existing])
    session = _session(cli)
    session.ensure_loaded()

    session.unload()

    assert [c for c in cli.commands if c[0] == "unload"] == []
    assert len(cli.loaded) == 2


def test_unload_without_a_load_does_nothing() -> None:
    cli = FakeCli(loaded=[USER_OWNED])

    _session(cli).unload()

    assert [c for c in cli.commands if c[0] == "unload"] == []


def test_unload_is_idempotent() -> None:
    cli = FakeCli()
    session = _session(cli)
    session.ensure_loaded()

    session.unload()
    session.unload()

    assert len([c for c in cli.commands if c[0] == "unload"]) == 1
    assert session.instance_id == ""


def test_session_never_issues_unload_all_or_a_download() -> None:
    cli = FakeCli(loaded=[USER_OWNED])
    session = _session(cli)

    session.ensure_loaded()
    session.unload()

    flat = [token for command in cli.commands for token in command]
    assert "--all" not in flat
    assert "-a" not in flat
    assert "get" not in [command[0] for command in cli.commands]


def test_context_manager_unloads_on_exit() -> None:
    cli = FakeCli(loaded=[USER_OWNED])

    with _session(cli) as session:
        assert session.owns_instance is True

    assert [m["identifier"] for m in cli.loaded] == ["gemma-4-26b-a4b-it"]


def test_context_manager_unloads_even_when_the_body_raises() -> None:
    cli = FakeCli(loaded=[USER_OWNED])

    with pytest.raises(ValueError):
        with _session(cli):
            raise ValueError("planning blew up")

    assert [m["identifier"] for m in cli.loaded] == ["gemma-4-26b-a4b-it"]


# --------------------------------------------------------------- redaction


def test_redaction_masks_authorization_values() -> None:
    redacted = redact_secrets("Authorization: Bearer sk-abc123 --api-key sk-def456")

    assert "sk-abc123" not in redacted
    assert "sk-def456" not in redacted
    assert "Authorization" in redacted


def test_command_logging_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    cli = FakeCli()
    session = _session(cli, identifier="planner", extra_load_args=["--api-key", "sk-topsecret"])

    with caplog.at_level(logging.INFO):
        session.ensure_loaded()

    assert "sk-topsecret" not in caplog.text
    assert "load" in caplog.text


def test_errors_do_not_leak_authorization_values() -> None:
    cli = FakeCli(fail_on="load")
    session = _session(cli, extra_load_args=["--api-key", "sk-topsecret"])

    with pytest.raises(LMStudioError) as excinfo:
        session.ensure_loaded()

    assert "sk-topsecret" not in str(excinfo.value)
