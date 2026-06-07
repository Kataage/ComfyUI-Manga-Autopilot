"""Tests for the layered configuration loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from manga_autopilot.config import (
    AppConfig,
    ConfigLoadError,
    default_config,
    discover_config_path,
    load_config,
)


def test_default_config_has_expected_defaults() -> None:
    cfg = default_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.app.language == "ja"
    assert cfg.comfyui.base_url == "http://127.0.0.1:8188"
    assert cfg.generation.default_candidate_count == 4
    assert cfg.security.allow_remote_comfyui is False


def test_load_config_returns_defaults_when_path_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert cfg == default_config()


def test_load_config_overrides_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
app:
  language: en
  autosave_interval_sec: 30
comfyui:
  base_url: http://192.168.1.10:8188
generation:
  default_candidate_count: 8
""".strip(),
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.app.language == "en"
    assert cfg.app.autosave_interval_sec == 30
    assert cfg.comfyui.base_url == "http://192.168.1.10:8188"
    assert cfg.generation.default_candidate_count == 8
    # untouched keys still come from defaults
    assert cfg.comfyui.client_id == "manga_autopilot_client"


def test_load_config_rejects_non_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- not\n- a\n- mapping", encoding="utf-8")
    with pytest.raises(ConfigLoadError):
        load_config(config_path)


def test_load_config_rejects_invalid_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
generation:
  default_candidate_count: not-a-number
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigLoadError):
        load_config(config_path)


def test_discover_config_path_prefers_yaml(tmp_path: Path) -> None:
    yml = tmp_path / "config.yml"
    yaml = tmp_path / "config.yaml"
    yml.write_text("app: {}", encoding="utf-8")
    yaml.write_text("app: {}", encoding="utf-8")
    assert discover_config_path(tmp_path) == yaml


def test_discover_config_path_returns_none_when_missing(tmp_path: Path) -> None:
    assert discover_config_path(tmp_path) is None
