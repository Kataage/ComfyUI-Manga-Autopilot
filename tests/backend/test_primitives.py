"""Tests for stable v2 ID, canonical JSON, and fingerprint primitives."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest

from manga_autopilot.primitives import (
    canonical_json,
    canonical_json_bytes,
    dependency_fingerprint,
    input_fingerprint,
    new_id,
    sha256_bytes,
    sha256_canonical,
    sha256_file,
    uuid7,
)


def test_uuid7_has_expected_version_variant_and_timestamp() -> None:
    before_ms = time.time_ns() // 1_000_000
    value = uuid7()
    after_ms = time.time_ns() // 1_000_000

    encoded_ms = value.int >> 80
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert before_ms <= encoded_ms <= after_ms


def test_prefixed_ids_are_opaque_unique_and_parseable() -> None:
    values = {new_id("panel") for _ in range(500)}

    assert len(values) == 500
    for value in values:
        prefix, raw_uuid = value.split("_", maxsplit=1)
        assert prefix == "panel"
        parsed = uuid.UUID(raw_uuid)
        assert parsed.version == 7


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "Panel",
        "panel_item",
        "panel-item",
        "1panel",
        "../panel",
        "panel/",
    ],
)
def test_new_id_rejects_invalid_prefix(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        new_id(prefix)


def test_canonical_json_is_independent_of_mapping_key_order() -> None:
    first = {
        "character": {"name": "リリア", "traits": ["blue eyes", "scar"]},
        "revision": 3,
        "active": True,
    }
    second = {
        "active": True,
        "revision": 3,
        "character": {"traits": ["blue eyes", "scar"], "name": "リリア"},
    }

    assert canonical_json(first) == canonical_json(second)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert sha256_canonical(first) == sha256_canonical(second)
    assert input_fingerprint(first) == input_fingerprint(second)


def test_canonical_json_uses_compact_utf8_safe_representation() -> None:
    payload = {"text": "日本語", "values": [1, 2, 3]}

    serialized = canonical_json(payload)

    assert serialized == '{"text":"日本語","values":[1,2,3]}'
    assert canonical_json_bytes(payload) == serialized.encode("utf-8")


def test_canonical_json_rejects_non_json_nan() -> None:
    with pytest.raises(ValueError):
        canonical_json({"score": float("nan")})


def test_canonical_json_rejects_non_serializable_values() -> None:
    with pytest.raises(TypeError):
        canonical_json({"value": object()})


def test_sha256_bytes_matches_stdlib() -> None:
    data = b"manga-autopilot"

    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_sha256_file_matches_bytes_hash(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    data = (b"panel-data-" * 4096) + b"end"
    path.write_bytes(data)

    assert sha256_file(path, chunk_size=97) == sha256_bytes(data)


def test_sha256_file_rejects_invalid_chunk_size(tmp_path: Path) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"x")

    with pytest.raises(ValueError, match="chunk_size"):
        sha256_file(path, chunk_size=0)


def test_input_fingerprint_has_explicit_algorithm_prefix() -> None:
    fingerprint = input_fingerprint({"panel": "panel_001", "revision": 4})

    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == len("sha256:") + 64


def test_dependency_fingerprint_is_stable_and_namespaced() -> None:
    first = {
        "page": {"id": "page_001", "revision": 2},
        "panel": {"id": "panel_001", "revision": 5},
    }
    second = {
        "panel": {"revision": 5, "id": "panel_001"},
        "page": {"revision": 2, "id": "page_001"},
    }

    assert dependency_fingerprint(first) == dependency_fingerprint(second)
    assert dependency_fingerprint(first) != input_fingerprint(first)


def test_canonical_output_can_be_parsed_as_equivalent_json() -> None:
    payload = {"b": [2, 1], "a": {"x": None}}

    assert json.loads(canonical_json(payload)) == payload
