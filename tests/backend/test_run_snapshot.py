from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest

from manga_autopilot.models.generation_profile import SemanticPromptSegments
from manga_autopilot.services.anima_prompt_builder import AnimaPromptBuilder
from manga_autopilot.services.generation_profiles import load_builtin_profile
from manga_autopilot.services.model_fingerprint import FingerprintCache, ModelFingerprint
from manga_autopilot.services.run_snapshot import (
    EnvironmentSnapshot,
    LLMSettingsSnapshot,
    PanelPromptSnapshot,
    RunSnapshot,
    RunSnapshotWriter,
    SecretLeakError,
    assert_no_secrets,
    hash_json_document,
    log_prompt_digest,
    scrub_secrets,
)

# --------------------------------------------------------------- fingerprints


def _write_model(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_fingerprint_matches_sha256_of_the_file(tmp_path: Path) -> None:
    payload = b"fake safetensors payload" * 100
    path = _write_model(tmp_path, "anima.safetensors", payload)

    fingerprint = FingerprintCache().fingerprint(path)

    assert isinstance(fingerprint, ModelFingerprint)
    assert fingerprint.sha256 == hashlib.sha256(payload).hexdigest()
    assert fingerprint.size == len(payload)
    assert fingerprint.name == "anima.safetensors"


def test_fingerprint_cache_reuses_the_previous_result(tmp_path: Path) -> None:
    path = _write_model(tmp_path, "anima.safetensors", b"abc")
    cache = FingerprintCache()

    first = cache.fingerprint(path)
    second = cache.fingerprint(path)

    assert first == second
    assert (cache.misses, cache.hits) == (1, 1)


def test_fingerprint_cache_invalidates_when_the_file_changes(tmp_path: Path) -> None:
    path = _write_model(tmp_path, "anima.safetensors", b"abc")
    cache = FingerprintCache()
    first = cache.fingerprint(path)

    path.write_bytes(b"a different payload of another length")
    second = cache.fingerprint(path)

    assert second.sha256 != first.sha256
    assert second.size != first.size
    assert cache.misses == 2


def test_fingerprint_of_a_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FingerprintCache().fingerprint(tmp_path / "absent.safetensors")


def test_fingerprinting_never_copies_the_model(tmp_path: Path) -> None:
    path = _write_model(tmp_path, "anima.safetensors", b"abc" * 1000)
    before = sorted(p.name for p in tmp_path.iterdir())

    FingerprintCache().fingerprint(path)

    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_fingerprint_does_not_carry_the_absolute_path(tmp_path: Path) -> None:
    path = _write_model(tmp_path, "anima.safetensors", b"abc")

    dumped = FingerprintCache().fingerprint(path).model_dump_json()

    assert str(tmp_path) not in dumped
    assert "path" not in json.loads(dumped)


# --------------------------------------------------------------------- hashes


def test_json_hash_is_stable_across_key_order() -> None:
    assert hash_json_document({"a": 1, "b": [2, 3]}) == hash_json_document({"b": [2, 3], "a": 1})
    assert hash_json_document({"a": 1}) != hash_json_document({"a": 2})
    assert len(hash_json_document({"a": 1})) == 64


def test_profile_hash_changes_with_the_profile() -> None:
    base = load_builtin_profile("anima_base").model_dump(mode="json")
    turbo = load_builtin_profile("anima_turbo").model_dump(mode="json")

    assert hash_json_document(base) != hash_json_document(turbo)


# -------------------------------------------------------------------- secrets


def test_scrub_secrets_removes_credentials_at_every_depth() -> None:
    scrubbed = scrub_secrets(
        {
            "model": "qwen",
            "api_key": "sk-secret",
            "nested": {"authorization": "Bearer abc", "temperature": 0.2},
            "list": [{"password": "hunter2", "keep": 1}],
        }
    )

    assert scrubbed == {
        "model": "qwen",
        "nested": {"temperature": 0.2},
        "list": [{"keep": 1}],
    }


def test_assert_no_secrets_rejects_a_document_carrying_a_token() -> None:
    with pytest.raises(SecretLeakError, match="api_key"):
        assert_no_secrets({"llm": {"api_key": "sk-secret"}})


def test_llm_settings_drop_token_values() -> None:
    settings = LLMSettingsSnapshot.from_mapping(
        {
            "provider": "lmstudio",
            "model": "qwen3.5-9b",
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.2,
            "api_key": "sk-should-never-be-stored",
        }
    )

    dumped = settings.model_dump_json()
    assert "sk-should-never-be-stored" not in dumped
    assert "api_key" not in dumped
    assert settings.model == "qwen3.5-9b"


# ------------------------------------------------------------------ snapshots


def _snapshot(tmp_path: Path, run_id: str = "run-1") -> RunSnapshot:
    profile = load_builtin_profile("anima_turbo")
    spec = AnimaPromptBuilder().render(
        SemanticPromptSegments(must_keep=["black bob hair"], subject=["1girl"]),
        profile,
        seed=4242,
        panel_size=(3, 4),
    )
    model = tmp_path / "anima.safetensors"
    model.write_bytes(b"weights")
    return RunSnapshot(
        run_id=run_id,
        project_id="proj-1",
        generation_profile_id=profile.id,
        profile_hash=hash_json_document(profile.model_dump(mode="json")),
        workflow_hash=hash_json_document({"nodes": []}),
        models=[FingerprintCache().fingerprint(model)],
        llm=LLMSettingsSnapshot.from_mapping(
            {"provider": "lmstudio", "model": "qwen3.5-9b", "api_key": "sk-x"}
        ),
        environment=EnvironmentSnapshot.capture(comfyui_version="0.30.0"),
        panels=[PanelPromptSnapshot.from_prompt_spec("p1-01", spec)],
    )


def test_snapshot_stores_the_complete_rendered_prompt_and_settings(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    panel = snapshot.panels[0]
    assert "black bob hair" in panel.positive
    assert "speech text in image" in panel.negative
    assert (panel.seed, panel.width, panel.height) == (4242, 960, 1280)
    assert (panel.steps, panel.cfg) == (12, 1)
    assert (panel.sampler, panel.scheduler) == ("er_sde", "simple")


def test_panel_prompt_hash_tracks_the_rendered_prompt() -> None:
    profile = load_builtin_profile("anima_turbo")
    segments = SemanticPromptSegments(subject=["1girl"])
    first = AnimaPromptBuilder().render(segments, profile, seed=1)
    same = AnimaPromptBuilder().render(segments, profile, seed=1)
    other_seed = AnimaPromptBuilder().render(segments, profile, seed=2)

    hash_first = PanelPromptSnapshot.from_prompt_spec("p1", first).prompt_hash
    assert hash_first == PanelPromptSnapshot.from_prompt_spec("p1", same).prompt_hash
    assert hash_first != PanelPromptSnapshot.from_prompt_spec("p1", other_seed).prompt_hash
    assert len(hash_first) == 64


def test_environment_snapshot_records_the_runtime() -> None:
    environment = EnvironmentSnapshot.capture(comfyui_version="0.30.0")

    assert environment.python_version.startswith("3.")
    assert environment.platform
    assert environment.comfyui_version == "0.30.0"


def test_writer_places_the_snapshot_in_the_run_directory(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    path = RunSnapshotWriter(tmp_path).write(snapshot)

    assert path == tmp_path / "runs" / "run-1" / "snapshot.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["run_id"] == "run-1"
    assert document["panels"][0]["prompt_hash"] == snapshot.panels[0].prompt_hash


def test_written_snapshot_round_trips(tmp_path: Path) -> None:
    writer = RunSnapshotWriter(tmp_path)
    original = _snapshot(tmp_path)
    writer.write(original)

    restored = writer.read("run-1")

    assert restored.panels[0].positive == original.panels[0].positive
    assert restored.models[0].sha256 == original.models[0].sha256


def test_written_snapshot_never_contains_credentials(tmp_path: Path) -> None:
    path = RunSnapshotWriter(tmp_path).write(_snapshot(tmp_path))

    raw = path.read_text(encoding="utf-8")
    assert "sk-x" not in raw
    assert "api_key" not in raw


def test_writer_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    RunSnapshotWriter(tmp_path).write(_snapshot(tmp_path))

    run_dir = tmp_path / "runs" / "run-1"
    assert [p.name for p in run_dir.iterdir()] == ["snapshot.json"]


def test_diagnostic_logging_uses_the_prompt_hash_not_the_prompt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    panel = _snapshot(tmp_path).panels[0]

    with caplog.at_level(logging.INFO):
        log_prompt_digest(logging.getLogger("manga_autopilot.test"), panel)

    assert panel.prompt_hash in caplog.text
    assert "black bob hair" not in caplog.text
    assert "1girl" not in caplog.text
