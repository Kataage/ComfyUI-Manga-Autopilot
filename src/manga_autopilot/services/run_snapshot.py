"""Per-run reproducibility snapshots.

``runs/{run_id}/snapshot.json`` records everything needed to reproduce a run:
the rendered prompts in full, the seeds and effective dimensions, the profile
and workflow hashes, the model fingerprints, the LLM settings, and the runtime
environment.

Two rules shape this module:

* Complete prompts live in the snapshot. Diagnostic logs carry only the prompt
  hash, so a debug log can be shared without shipping the prompt text.
* Credentials never reach disk. Settings are scrubbed on the way in, and the
  writer refuses to serialise a document that still carries a secret-looking key.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from manga_autopilot.services.model_fingerprint import ModelFingerprint
from manga_autopilot.services.prompt_builder import PromptSpec

log = logging.getLogger(__name__)

SNAPSHOT_FILENAME = "snapshot.json"
CURRENT_SNAPSHOT_SCHEMA_VERSION = 1

#: Key names that always denote a credential.
SECRET_KEY_NAMES: frozenset[str] = frozenset(
    {
        "api_key",
        "access_token",
        "auth_token",
        "authentication",
        "authorization",
        "bearer_token",
        "credential",
        "credentials",
        "id_token",
        "passphrase",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "session_token",
        "token",
    }
)

#: Suffixes that make a key a credential. Deliberately singular: ``max_tokens``
#: is a length budget, ``access_token`` is a credential.
SECRET_KEY_SUFFIXES: tuple[str, ...] = (
    "_key",
    "_token",
    "_secret",
    "_password",
    "_passphrase",
    "_credential",
)


class SecretLeakError(ValueError):
    """Raised when a document about to be persisted still carries a credential."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_key(key: str) -> str:
    """Fold ``apiKey``, ``API-KEY`` and ``api key`` onto ``api_key``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


def _is_secret_key(key: object) -> bool:
    """Return whether `key` names a credential rather than an ordinary setting."""
    if not isinstance(key, str):
        return False
    normalised = _normalise_key(key)
    if normalised in SECRET_KEY_NAMES:
        return True
    return normalised.endswith(SECRET_KEY_SUFFIXES)


def scrub_secrets(value: Any) -> Any:
    """Return `value` with every secret-looking mapping key removed, at any depth."""
    if isinstance(value, Mapping):
        return {k: scrub_secrets(v) for k, v in value.items() if not _is_secret_key(k)}
    if isinstance(value, (list, tuple)):
        return [scrub_secrets(item) for item in value]
    return value


def assert_no_secrets(document: Any, _path: str = "") -> None:
    """Raise :class:`SecretLeakError` if `document` contains a secret-looking key.

    Raises:
        SecretLeakError: naming the offending key path.
    """
    if isinstance(document, Mapping):
        for key, item in document.items():
            if _is_secret_key(key):
                location = f"{_path}.{key}" if _path else str(key)
                raise SecretLeakError(f"refusing to persist credential-like key: {location}")
            assert_no_secrets(item, f"{_path}.{key}" if _path else str(key))
    elif isinstance(document, (list, tuple)):
        for index, item in enumerate(document):
            assert_no_secrets(item, f"{_path}[{index}]")


def hash_json_document(document: Any) -> str:
    """Return a stable SHA-256 of `document`, independent of key order."""
    import hashlib

    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LLMSettingsSnapshot(BaseModel):
    """Planner settings worth recording. Credentials are not among them."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=512)
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    response_format: str = Field(default="", max_length=64)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LLMSettingsSnapshot:
        """Build a snapshot from a raw provider config, dropping credentials."""
        scrubbed = scrub_secrets(dict(data))
        dropped = sorted(set(data) - set(scrubbed))
        if dropped:
            log.debug("dropped credential-like LLM settings: %s", dropped)
        known = {name for name in cls.model_fields}
        return cls(**{k: v for k, v in scrubbed.items() if k in known})


class PlannerCostSnapshot(BaseModel):
    """What the planner cost this run, so two runs can be compared.

    Planner latency dominates a strict run and swings by an order of magnitude
    for the same prompt. `reasoning_ratio` is the share of generated characters
    spent on chain of thought that is then discarded - the number that decides
    whether a faster model is worth switching to.
    """

    model_config = ConfigDict(extra="forbid")

    calls: int = 0
    seconds: float = 0.0
    reasoning_chars: int = 0
    content_chars: int = 0
    reasoning_ratio: float = 0.0


class EnvironmentSnapshot(BaseModel):
    """Runtime the run executed on."""

    model_config = ConfigDict(extra="forbid")

    python_version: str = ""
    platform: str = ""
    extension_version: str = ""
    comfyui_version: str = ""
    torch_version: str = ""
    gpu_name: str = ""

    @classmethod
    def capture(
        cls,
        *,
        extension_version: str = "",
        comfyui_version: str = "",
        torch_version: str = "",
        gpu_name: str = "",
    ) -> EnvironmentSnapshot:
        """Capture the local runtime. Caller-supplied values are trusted as-is."""
        return cls(
            python_version=platform.python_version(),
            platform=f"{platform.system()} {platform.release()}",
            extension_version=extension_version,
            comfyui_version=comfyui_version,
            torch_version=torch_version,
            gpu_name=gpu_name,
        )


class PanelPromptSnapshot(BaseModel):
    """The complete render request for one panel."""

    model_config = ConfigDict(extra="forbid")

    panel_id: str = Field(min_length=1, max_length=128)
    positive: str = ""
    negative: str = ""
    seed: int = 0
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    cfg: float = 0.0
    sampler: str = ""
    scheduler: str = ""
    prompt_hash: str = Field(default="", max_length=64)

    @classmethod
    def from_prompt_spec(cls, panel_id: str, spec: PromptSpec) -> PanelPromptSnapshot:
        """Record `spec` as it will reach the sampler, bans included."""
        snapshot = cls(
            panel_id=panel_id,
            positive=spec.positive,
            negative=spec.negative_full(),
            seed=spec.seed,
            width=spec.width,
            height=spec.height,
            steps=spec.steps,
            cfg=spec.cfg,
            sampler=spec.sampler,
            scheduler=spec.scheduler,
        )
        payload = snapshot.model_dump(mode="json")
        payload.pop("prompt_hash", None)
        payload.pop("panel_id", None)
        return snapshot.model_copy(update={"prompt_hash": hash_json_document(payload)})


class RunSnapshot(BaseModel):
    """Everything needed to reproduce one run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    schema_version: int = CURRENT_SNAPSHOT_SCHEMA_VERSION
    created_at: str = Field(default_factory=_utc_now_iso)
    generation_profile_id: str = ""
    profile_hash: str = ""
    workflow_hash: str = ""
    models: list[ModelFingerprint] = Field(default_factory=list)
    llm: LLMSettingsSnapshot | None = None
    planner: PlannerCostSnapshot | None = None
    environment: EnvironmentSnapshot = Field(default_factory=EnvironmentSnapshot)
    panels: list[PanelPromptSnapshot] = Field(default_factory=list)


@dataclass
class RunSnapshotWriter:
    """Read and write ``runs/{run_id}/snapshot.json`` under a project root."""

    project_root: Path

    def path_for(self, run_id: str) -> Path:
        return Path(self.project_root) / "runs" / run_id / SNAPSHOT_FILENAME

    def write(self, snapshot: RunSnapshot) -> Path:
        """Persist `snapshot` atomically.

        Raises:
            SecretLeakError: the serialised snapshot carries a credential-like key.
        """
        document = snapshot.model_dump(mode="json")
        assert_no_secrets(document)

        target = self.path_for(snapshot.run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(
                json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp.replace(target)
        finally:
            if temp.exists():
                temp.unlink()

        log.info(
            "wrote run snapshot for %s (%d panels, profile %s)",
            snapshot.run_id,
            len(snapshot.panels),
            snapshot.generation_profile_id or "-",
        )
        return target

    def read(self, run_id: str) -> RunSnapshot:
        path = self.path_for(run_id)
        return RunSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))


def log_prompt_digest(
    logger: logging.Logger,
    panel: PanelPromptSnapshot,
    *,
    message: str = "panel %s prompt %s (%dx%d, seed %d)",
) -> None:
    """Log a panel's render request by hash. The prompt text never reaches the log."""
    logger.info(
        message,
        panel.panel_id,
        panel.prompt_hash,
        panel.width,
        panel.height,
        panel.seed,
    )


def snapshot_panel_prompts(specs: Sequence[tuple[str, PromptSpec]]) -> list[PanelPromptSnapshot]:
    """Convenience wrapper for building panel snapshots from ``(panel_id, spec)`` pairs."""
    return [PanelPromptSnapshot.from_prompt_spec(panel_id, spec) for panel_id, spec in specs]


__all__ = [
    "CURRENT_SNAPSHOT_SCHEMA_VERSION",
    "SECRET_KEY_NAMES",
    "SECRET_KEY_SUFFIXES",
    "SNAPSHOT_FILENAME",
    "EnvironmentSnapshot",
    "LLMSettingsSnapshot",
    "PlannerCostSnapshot",
    "PanelPromptSnapshot",
    "RunSnapshot",
    "RunSnapshotWriter",
    "SecretLeakError",
    "assert_no_secrets",
    "hash_json_document",
    "log_prompt_digest",
    "scrub_secrets",
    "snapshot_panel_prompts",
]
