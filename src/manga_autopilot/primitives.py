"""Stable v2 primitives for IDs, canonical JSON, hashes, and fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

_ID_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]*$")
_SHA256_PREFIX = "sha256:"


def uuid7() -> uuid.UUID:
    """Return an RFC 9562-compatible UUIDv7 value.

    Python 3.10/3.11 do not provide :func:`uuid.uuid7`, so the project keeps
    this small implementation local until the minimum runtime provides one.
    """
    unix_ts_ms = int(time.time_ns() // 1_000_000)
    if unix_ts_ms >= 1 << 48:
        raise OverflowError("current Unix timestamp does not fit UUIDv7")

    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    value = unix_ts_ms << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def new_id(prefix: str) -> str:
    """Create one opaque prefixed persistent ID."""
    if not _ID_PREFIX_RE.fullmatch(prefix):
        raise ValueError(
            "prefix must start with a lowercase letter and contain only "
            "lowercase ASCII letters or digits"
        )
    return f"{prefix}_{uuid7()}"


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value deterministically for hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return UTF-8 bytes of :func:`canonical_json`."""
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest for bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading the full file into memory."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_canonical(value: Any) -> str:
    """Hash a canonical JSON payload."""
    return sha256_bytes(canonical_json_bytes(value))


def input_fingerprint(value: Any) -> str:
    """Return a version-independent content fingerprint for an input payload."""
    return _SHA256_PREFIX + sha256_canonical(value)


def dependency_fingerprint(dependencies: Any) -> str:
    """Fingerprint one dependency payload under an explicit dependency envelope."""
    return input_fingerprint({"dependencies": dependencies})


__all__ = [
    "canonical_json",
    "canonical_json_bytes",
    "dependency_fingerprint",
    "input_fingerprint",
    "new_id",
    "sha256_bytes",
    "sha256_canonical",
    "sha256_file",
    "uuid7",
]
