"""SHA-256 fingerprints for the model files a run depended on.

A run snapshot records which weights produced an image. Hashing a multi-gigabyte
checkpoint is slow, so results are cached per resolved path and invalidated when
the file's size or mtime changes. The model itself is only ever read, never
copied, and its absolute path never reaches the fingerprint: the snapshot keeps
the file name, size, and digest, which is what reproducibility needs.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 1024 * 1024


class ModelFingerprint(BaseModel):
    """Identity of one model file, safe to store in a snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=256)
    size: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)

    @property
    def short(self) -> str:
        return self.sha256[:12]


def sha256_file(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Return the SHA-256 of `path`, read in chunks so large weights fit in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class FingerprintCache:
    """Fingerprint model files, reusing results while a file is unchanged."""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    hits: int = 0
    misses: int = 0
    _entries: dict[str, tuple[int, int, ModelFingerprint]] = field(default_factory=dict)

    def fingerprint(self, path: Path) -> ModelFingerprint:
        """Return the fingerprint of `path`.

        Raises:
            FileNotFoundError: the model file does not exist.
        """
        resolved = Path(path).resolve()
        stat = resolved.stat()  # raises FileNotFoundError for a missing model
        key = str(resolved)

        cached = self._entries.get(key)
        if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            self.hits += 1
            return cached[2]

        self.misses += 1
        fingerprint = ModelFingerprint(
            name=resolved.name,
            size=stat.st_size,
            sha256=sha256_file(resolved, self.chunk_size),
        )
        self._entries[key] = (stat.st_size, stat.st_mtime_ns, fingerprint)
        log.info("fingerprinted %s (%d bytes, %s)", fingerprint.name, fingerprint.size, fingerprint.short)
        return fingerprint

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0


#: ComfyUI's own ``models/`` subdirectories, and the ``extra_model_paths.yaml``
#: keys that mean the same thing, per asset kind. Desktop installs put nothing
#: under ``models/`` and list every location in the YAML instead, so both have
#: to be consulted.
COMFY_MODEL_SUBDIRS: dict[str, tuple[str, ...]] = {
    "unet": ("unet", "diffusion_models", "checkpoints"),
    "text_encoder": ("text_encoders", "clip"),
    "vae": ("vae",),
    "lora": ("loras",),
}

EXTRA_PATHS_FILENAME = "extra_model_paths.yaml"


def _extra_model_paths(install_root: Path, keys: Sequence[str]) -> list[Path]:
    """Return the directories `extra_model_paths.yaml` lists for `keys`.

    The file maps a profile name to a mapping of key -> one or more newline
    separated directories. A malformed or absent file yields nothing; this is a
    best-effort lookup for a snapshot field, never a hard requirement.
    """
    config = Path(install_root) / EXTRA_PATHS_FILENAME
    if not config.is_file():
        return []
    try:
        import yaml

        document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - a bad config must not fail a run
        log.warning("could not read %s: %s", config, exc)
        return []

    found: list[Path] = []
    for section in document.values():
        if not isinstance(section, dict):
            continue
        for key in keys:
            entry = section.get(key)
            if not isinstance(entry, str):
                continue
            found.extend(Path(line.strip()) for line in entry.splitlines() if line.strip())
    return found


def resolve_model_path(install_root: Path | str, kind: str, name: str) -> Path | None:
    """Find the file `name` for a `kind` of asset under a ComfyUI install.

    Looks in ``models/<sub>`` and in whatever ``extra_model_paths.yaml`` lists.
    Returns ``None`` when the file is nowhere to be found, which is a normal
    outcome for an install that keeps its weights somewhere else entirely.
    """
    root = Path(install_root)
    subs = COMFY_MODEL_SUBDIRS.get(kind, ())
    candidates = [root / "models" / sub for sub in subs]
    candidates.extend(_extra_model_paths(root, subs))
    for directory in candidates:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def fingerprint_profile_models(
    profile: Any,
    install_root: Path | str | None,
    cache: FingerprintCache | None = None,
) -> list[ModelFingerprint]:
    """Fingerprint the model files a profile declares.

    Returns an empty list when `install_root` is unset: ComfyUI reports model
    names but not their paths, so without a configured install there is nothing
    to hash. A file that cannot be found is logged and skipped rather than
    guessed at - a snapshot with a wrong digest is worse than one with none.
    """
    if not install_root:
        log.info("no ComfyUI install root configured; skipping model fingerprints")
        return []

    cache = cache or FingerprintCache()
    assets = profile.assets
    wanted: list[tuple[str, str]] = [
        ("unet", assets.unet),
        ("text_encoder", assets.text_encoder),
        ("vae", assets.vae),
        *[("lora", lora.name) for lora in assets.loras],
    ]

    fingerprints: list[ModelFingerprint] = []
    for kind, name in wanted:
        if not name:
            continue
        path = resolve_model_path(install_root, kind, name)
        if path is None:
            log.warning("model %s not found under %s; not fingerprinted", name, install_root)
            continue
        fingerprints.append(cache.fingerprint(path))
    return fingerprints


__all__ = [
    "COMFY_MODEL_SUBDIRS",
    "EXTRA_PATHS_FILENAME",
    "DEFAULT_CHUNK_SIZE",
    "FingerprintCache",
    "ModelFingerprint",
    "fingerprint_profile_models",
    "resolve_model_path",
    "sha256_file",
]
