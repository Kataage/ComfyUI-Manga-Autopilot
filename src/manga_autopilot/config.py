"""Configuration loading for Manga Autopilot.

Spec reference: ``docs/comfyui_manga_autopilot_spec.md`` section 27.

The configuration is layered as follows (highest priority first):

    Project settings  >  User config.yaml  >  Built-in defaults

This module is responsible only for the "User config.yaml > defaults" merge.
Per-project overrides live with the project model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

DEFAULT_CONFIG_FILENAMES = ("config.yaml", "config.yml")


class AppSettings(BaseModel):
    name: str = "ComfyUI Manga Autopilot"
    language: str = "ja"
    storage_path: str = "./user_data/manga_autopilot"
    autosave_interval_sec: int = 10


class ComfyUISettings(BaseModel):
    base_url: str = "http://127.0.0.1:8188"
    timeout_sec: int = 600
    use_websocket: bool = True
    client_id: str = "manga_autopilot_client"


class GenerationSettings(BaseModel):
    default_candidate_count: int = 4
    max_retry_per_panel: int = 5
    quality_threshold: float = 0.78
    default_width: int = 768
    default_height: int = 1024
    default_steps: int = 28
    default_cfg: float = 7.0
    default_sampler: str = "dpmpp_2m"
    default_scheduler: str = "karras"


class CharacterSettings(BaseModel):
    use_reference: bool = True
    use_lora: bool = False
    default_reference_strength: float = 0.65
    generate_character_sheet: bool = True


class LLMSettings(BaseModel):
    provider: str = "ollama"
    endpoint: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b-instruct"
    temperature: float = 0.7
    max_tokens: int = 4096


class ModalSettings(BaseModel):
    enabled: bool = False
    endpoint: str = ""
    api_key_env: str = "MODAL_MANGA_AUTOPILOT_KEY"
    delete_temp_after_return: bool = True
    return_type: str = "base64"
    timeout_sec: int = 900


class ExportSettings(BaseModel):
    webtoon_width: int = 1080
    max_webtoon_slice_height: int = 12000
    pdf_size: str = "A4"
    dpi: int = 300


class SecuritySettings(BaseModel):
    allow_remote_comfyui: bool = False
    warn_unknown_custom_nodes: bool = True
    warn_external_network_nodes: bool = True
    mask_prompt_in_logs: bool = False


class AppConfig(BaseModel):
    """Top-level Manga Autopilot configuration."""

    app: AppSettings = Field(default_factory=AppSettings)
    comfyui: ComfyUISettings = Field(default_factory=ComfyUISettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    character: CharacterSettings = Field(default_factory=CharacterSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    modal: ModalSettings = Field(default_factory=ModalSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)


class ConfigLoadError(ValueError):
    """Raised when a configuration file exists but cannot be parsed."""


def default_config() -> AppConfig:
    """Return the built-in default configuration."""
    return AppConfig()


def _read_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigLoadError(f"Top-level config must be a mapping; got {type(data).__name__}")
    return data


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge two mapping trees, with ``override`` winning leaf conflicts."""

    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from ``path`` and merge with defaults.

    If ``path`` is ``None`` no file is consulted; only defaults are returned.
    If ``path`` refers to a file that does not exist, defaults are returned.
    If parsing fails, :class:`ConfigLoadError` is raised so callers can decide
    whether to fall back to defaults or surface the error to the user.
    """

    defaults = default_config().model_dump(mode="python")
    if path is None:
        return AppConfig(**defaults)

    path = Path(path)
    if not path.exists():
        return AppConfig(**defaults)

    override = _read_yaml(path)
    merged = _merge(defaults, override)
    try:
        return AppConfig(**merged)
    except ValidationError as exc:  # pragma: no cover - exercised via tests
        raise ConfigLoadError(f"Invalid configuration in {path}: {exc}") from exc


def discover_config_path(search_root: str | Path) -> Path | None:
    """Find the first ``config.yaml``/``config.yml`` under ``search_root``."""

    root = Path(search_root)
    for name in DEFAULT_CONFIG_FILENAMES:
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


__all__ = [
    "AppConfig",
    "AppSettings",
    "CharacterSettings",
    "ComfyUISettings",
    "ConfigLoadError",
    "ExportSettings",
    "GenerationSettings",
    "LLMSettings",
    "ModalSettings",
    "SecuritySettings",
    "default_config",
    "discover_config_path",
    "load_config",
]
