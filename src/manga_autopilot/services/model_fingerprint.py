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
from dataclasses import dataclass, field
from pathlib import Path

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


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "FingerprintCache",
    "ModelFingerprint",
    "sha256_file",
]
