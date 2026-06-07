"""LLM provider interface (spec sections 23.1, 23.2, 23.4).

Provides:

- :class:`LLMProvider` abstract base with async ``complete(prompt, schema)``
- :class:`LLMSettings` Pydantic model for provider configuration
- Concrete providers: ``ManualProvider``, ``OllamaProvider``, ``OpenAICompatibleProvider``
- :func:`build_provider` factory that resolves a settings instance to a class
- :func:`enforce_json_schema` helper to repair malformed LLM output (23.4)
"""

from __future__ import annotations

import abc
import json
import logging
import os
import re
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

ProviderType = Literal["local", "ollama", "openai_compatible", "manual"]


class LLMSettings(BaseModel):
    type: ProviderType = "manual"
    endpoint: str | None = None
    model: str = "manual"
    api_key_env: str | None = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=64, le=32768)

    @model_validator(mode="after")
    def _check_endpoint(self) -> LLMSettings:
        if self.type in {"ollama", "openai_compatible"} and not self.endpoint:
            raise ValueError(f"provider {self.type!r} requires an endpoint URL")
        return self

    @property
    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)


class LLMProvider(abc.ABC):
    """Abstract async LLM provider."""

    settings: LLMSettings

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    @abc.abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
    ) -> str:
        """Return the raw LLM text response."""

    async def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
        max_repair_attempts: int = 1,
    ) -> dict[str, Any]:
        """Run ``complete`` and validate against ``schema`` (with one repair)."""

        text = await self.complete(prompt, schema=schema, system=system)
        try:
            return _validate_json(text, schema)
        except ValueError as exc:
            if max_repair_attempts <= 0:
                raise
            log.warning("LLM JSON invalid, requesting repair: %s", exc)
            repair_prompt = _build_repair_prompt(text, schema, str(exc))
            text2 = await self.complete(repair_prompt, schema=schema, system=system)
            return _validate_json(text2, schema)


class ManualProvider(LLMProvider):
    """No-op provider used when the operator supplies plans manually."""

    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
    ) -> str:
        # For the manual provider we surface an empty JSON object so the
        # pipeline can be exercised end-to-end without an actual LLM.
        if schema is not None and "properties" in schema:
            return "{}"
        return ""


class OllamaProvider(LLMProvider):
    """Provider that talks to a local Ollama server."""

    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
    ) -> str:
        if not self.settings.endpoint:
            raise RuntimeError("OllamaProvider requires an endpoint")
        url = self.settings.endpoint.rstrip("/") + "/api/generate"
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.settings.temperature,
                "num_predict": self.settings.max_tokens,
            },
        }
        if system:
            payload["system"] = system
        if schema is not None:
            payload["format"] = schema
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return data.get("response", "")


class OpenAICompatibleProvider(LLMProvider):
    """Provider that talks to any OpenAI-compatible chat-completions endpoint."""

    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
    ) -> str:
        if not self.settings.endpoint:
            raise RuntimeError("OpenAICompatibleProvider requires an endpoint")
        url = self.settings.endpoint.rstrip("/") + "/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "")


def build_provider(settings: LLMSettings) -> LLMProvider:
    if settings.type == "manual":
        return ManualProvider(settings)
    if settings.type == "ollama":
        return OllamaProvider(settings)
    if settings.type in {"openai_compatible", "local"}:
        return OpenAICompatibleProvider(settings)
    raise ValueError(f"unsupported LLM provider: {settings.type!r}")


# ---------------------------------------------------------------- JSON utils
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Extract a JSON value from text, tolerating markdown fences."""

    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    match = _JSON_FENCE_RE.search(text)
    if match:
        return json.loads(match.group(1))
    # Find the first { or [ and try to parse from there.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        end = text.rfind(closer)
        if end == -1 or end < start:
            continue
        return json.loads(text[start : end + 1])
    raise ValueError(f"could not extract JSON from LLM response: {text!r}")


def _validate_json(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    required = schema.get("required") or []
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"missing required keys: {missing}")
    return data


def _build_repair_prompt(text: str, schema: dict[str, Any], error: str) -> str:
    return (
        "The previous JSON failed to validate against the required schema.\n"
        f"Error: {error}\n"
        "Repair the following JSON so that it satisfies the schema.\n"
        "Return only the repaired JSON. Do not include any commentary.\n\n"
        f"Schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"Previous JSON:\n{text}\n"
    )


def enforce_json_schema(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Public helper that wraps :func:`_validate_json` (spec section 23.4)."""

    return _validate_json(text, schema)


__all__ = [
    "LLMSettings",
    "LLMProvider",
    "ManualProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "build_provider",
    "enforce_json_schema",
]
