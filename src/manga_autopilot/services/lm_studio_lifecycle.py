"""Managed LM Studio model lifecycle for strict Anima planning.

Strict planning wants a specific model loaded, and wants the VRAM back once the
Storyboard is approved. That has to happen without disturbing whatever the user
is running themselves, so this session follows two rules:

* It unloads exactly one instance: the one it created, addressed by the
  identifier it assigned. ``lms unload --all`` is never issued, and a model the
  user already had loaded is adopted read-only rather than reloaded or evicted.
* It never downloads. ``lms get`` is not part of the vocabulary here; a missing
  model is an error the user resolves.

The command surface was read off the installed LM Studio CLI on 2026-08-26:
``lms load <model-key> [--identifier X] [--ttl N] [--context-length N] [--gpu R] -y``,
``lms unload <identifier>``, and ``lms ps --json``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Patterns whose captured value is replaced before anything is logged or raised.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?(\S+)"),
    re.compile(r"(?i)(bearer\s+)(\S+)"),
    re.compile(r"(?i)(--(?:api[-_]?key|token|password)[\s=]+)(\S+)"),
)

REDACTED = "***"


class LMStudioError(RuntimeError):
    """Raised when an ``lms`` invocation fails or returns something unusable."""


def redact_secrets(text: str) -> str:
    """Mask authorization values so a command or error can be logged safely."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", result)
    return result


@dataclass(frozen=True)
class LoadedModel:
    """One entry from ``lms ps --json``."""

    identifier: str
    model_key: str
    status: str = ""


def run_lms_cli(argv: Sequence[str], *, executable: str = "lms", timeout: int = 300) -> str:
    """Run the LM Studio CLI and return stdout.

    Raises:
        LMStudioError: the CLI is absent, timed out, or exited non-zero.
    """
    command = [executable, *argv]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise LMStudioError(f"LM Studio CLI {executable!r} was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise LMStudioError(f"lms {argv[0]} timed out after {timeout}s") from exc
    if completed.returncode != 0:
        raise LMStudioError(
            f"lms {argv[0]} failed ({completed.returncode}): "
            f"{redact_secrets(completed.stderr.strip() or completed.stdout.strip())}"
        )
    return completed.stdout


@dataclass
class ManagedLMStudioSession:
    """Load the planner model for the duration of a run, then give the VRAM back."""

    run: Callable[[list[str]], str]
    model_key: str
    identifier: str = "manga-autopilot-planner"
    ttl_seconds: int = 900
    context_length: int = 0
    gpu_offload: str = ""
    extra_load_args: Sequence[str] = field(default_factory=tuple)

    instance_id: str = field(default="", init=False)
    owns_instance: bool = field(default=False, init=False)

    # ------------------------------------------------------------- inspect
    def list_loaded(self) -> list[LoadedModel]:
        """Return the models LM Studio currently holds in memory."""
        raw = self._invoke(["ps", "--json"])
        try:
            payload = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise LMStudioError(f"lms ps returned output that is not JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise LMStudioError("lms ps did not return a list")
        return [
            LoadedModel(
                identifier=str(entry.get("identifier", "")),
                model_key=str(entry.get("modelKey", "")),
                status=str(entry.get("status", "")),
            )
            for entry in payload
            if isinstance(entry, dict)
        ]

    # ---------------------------------------------------------------- load
    def ensure_loaded(self) -> LoadedModel:
        """Make sure the planner model is available, loading it only if needed.

        An instance the user already had loaded is adopted and marked not-owned,
        so :meth:`unload` will leave it alone.
        """
        if self.instance_id:
            return LoadedModel(self.instance_id, self.model_key)

        for model in self.list_loaded():
            if model.model_key == self.model_key or model.identifier == self.identifier:
                self.instance_id = model.identifier
                self.owns_instance = False
                log.info(
                    "adopting already-loaded LM Studio instance %s (%s); it will not be unloaded",
                    model.identifier,
                    model.model_key,
                )
                return model

        argv = ["load", self.model_key, "--identifier", self.identifier, "-y"]
        if self.ttl_seconds > 0:
            argv += ["--ttl", str(self.ttl_seconds)]
        if self.context_length > 0:
            argv += ["--context-length", str(self.context_length)]
        if self.gpu_offload:
            argv += ["--gpu", self.gpu_offload]
        argv += list(self.extra_load_args)

        self._invoke(argv)
        self.instance_id = self.identifier
        self.owns_instance = True
        return LoadedModel(self.identifier, self.model_key)

    # -------------------------------------------------------------- unload
    def unload(self) -> bool:
        """Unload the instance this session created. Returns whether it did.

        Adopted instances and a session that never loaded anything are no-ops,
        which is what keeps a user's own model safe.
        """
        if not self.instance_id or not self.owns_instance:
            if self.instance_id:
                log.info(
                    "leaving LM Studio instance %s loaded; this session did not create it",
                    self.instance_id,
                )
            return False

        self._invoke(["unload", self.instance_id])
        log.info("unloaded LM Studio instance %s", self.instance_id)
        self.instance_id = ""
        self.owns_instance = False
        return True

    # ------------------------------------------------------------ contexts
    def __enter__(self) -> ManagedLMStudioSession:
        self.ensure_loaded()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.unload()

    # --------------------------------------------------------------- utils
    def _invoke(self, argv: list[str]) -> str:
        if "--all" in argv or "-a" in argv:
            raise LMStudioError("refusing to run a bulk lms command that affects other models")
        if argv and argv[0] == "get":
            raise LMStudioError("refusing to download a model; install it in LM Studio first")
        log.info("lms %s", redact_secrets(" ".join(argv)))
        try:
            return self.run(argv)
        except LMStudioError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalised and redacted below
            raise LMStudioError(
                f"lms {argv[0]} failed: {redact_secrets(str(exc))}"
            ) from None


__all__ = [
    "REDACTED",
    "LMStudioError",
    "LoadedModel",
    "ManagedLMStudioSession",
    "redact_secrets",
    "run_lms_cli",
]
