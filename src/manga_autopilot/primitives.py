"""Stable v2 primitives for IDs, canonical JSON, hashes, and fingerprints."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

_ID_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]*$")
CANONICAL_JSON_VERSION = 2
FINGERPRINT_VERSION = 2
_SHA256_PREFIX = f"sha256:v{FINGERPRINT_VERSION}:"


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


def _canonical_float(value: float) -> str:
    """Serialize one finite binary64 value without runtime float formatting."""
    if not math.isfinite(value):
        raise ValueError("canonical JSON does not support NaN or Infinity")
    if value == 0.0:
        return "0"

    numerator, denominator = value.as_integer_ratio()
    sign = "-" if numerator < 0 else ""
    numerator = abs(numerator)

    if denominator == 1:
        return sign + str(numerator)

    # A Python float is binary64. Its exact reduced denominator is a power of
    # two, so n / 2**k == (n * 5**k) / 10**k. Rendering that exact decimal
    # value avoids depending on Python repr()/JSON float formatting policy.
    exponent = denominator.bit_length() - 1
    if denominator != 1 << exponent:
        raise AssertionError("float denominator must be a power of two")

    digits = str(numerator * (5**exponent))
    if len(digits) <= exponent:
        rendered = "0." + ("0" * (exponent - len(digits))) + digits
    else:
        rendered = digits[:-exponent] + "." + digits[-exponent:]
    return sign + rendered


def _canonical_json_encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return _canonical_float(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False)
            + ":"
            + _canonical_json_encode(value[key])
            for key in sorted(value)
        ) + "}"

    raise TypeError(
        "unsupported canonical JSON type: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def canonical_json(value: Any) -> str:
    """Serialize a JSON value under the v2 canonicalization contract.

    Numeric rules are explicit: integers use exact base-10 form; finite floats
    use the exact decimal value of their IEEE-754 binary64 representation;
    integral floats collapse to the integer form; and all signed zero forms
    collapse to ``0``. NaN, Infinity, Decimal-like values, custom numeric
    classes, and non-string object keys are rejected.
    """
    return _canonical_json_encode(value)


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
    """Return a v2 content fingerprint for an input payload.

    The ``sha256:v2:`` prefix is part of the persistence contract. Historical
    ``sha256:<digest>`` values are v1 fingerprints and must not be compared as
    though they were generated by this canonicalization version.
    """
    return _SHA256_PREFIX + sha256_canonical(value)


def dependency_fingerprint(dependencies: Any) -> str:
    """Fingerprint one dependency payload under an explicit dependency envelope."""
    return input_fingerprint({"dependencies": dependencies})


__all__ = [
    "CANONICAL_JSON_VERSION",
    "FINGERPRINT_VERSION",
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
