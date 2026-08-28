"""LLM provider interface (spec sections 23.1, 23.2, 23.4).

Provides:

- :class:`LLMProvider` abstract base with async ``complete(prompt, schema)``
- :class:`LLMSettings` Pydantic model for provider configuration
- Concrete providers: ``ManualProvider``, ``OllamaProvider``, ``OpenAICompatibleProvider``
- :func:`build_provider` factory that resolves a settings instance to a class
- :func:`enforce_json_schema` helper to repair malformed LLM output (23.4)
- :class:`RepairOutcome` and :class:`JSONRepairLoop` for configurable retry+repair
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import aiohttp
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

ProviderType = Literal["local", "ollama", "openai_compatible", "manual"]


def chat_completions_url(endpoint: str) -> str:
    """Return the chat-completions URL for `endpoint`.

    Accepts a base URL with or without a trailing ``/v1``. Appending a second
    one produced ``/v1/v1/chat/completions``, which LM Studio answers with
    HTTP 200 and an error body rather than a 404.
    """
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def json_schema_response_format(
    name: str,
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": dict(schema),
        },
    }


class LLMSettings(BaseModel):
    type: ProviderType = "manual"
    endpoint: str | None = None
    model: str = "manual"
    api_key_env: str | None = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=64, le=32768)
    timeout_sec: int = Field(default=900, ge=1, le=7200)
    """How long to wait for one completion.

    aiohttp defaults to 300s, which a local reasoning model can exceed on a
    single planning call: measured at 297s for a two-page story plan, so the
    default was close enough to trip intermittently - and the timeout surfaced
    as an exception with an empty message.
    """

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
        semantic_validator: Callable[[dict[str, Any]], Sequence[str]] | None = None,
    ) -> dict[str, Any]:
        """Run ``complete`` and validate against ``schema`` (with up to N repair attempts)."""

        loop = JSONRepairLoop(
            max_repair_attempts=max_repair_attempts,
            system_prompt=system,
        )
        outcome = await loop.run(
            self,
            prompt,
            schema,
            semantic_validator=semantic_validator,
        )
        if not outcome.ok:
            raise ValueError(outcome.error or "JSON repair exhausted")
        return outcome.data


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
        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"{url} did not answer within {self.settings.timeout_sec}s"
                ) from exc
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
        url = chat_completions_url(self.settings.endpoint)
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
            payload["response_format"] = json_schema_response_format(
                "manga_autopilot_response",
                schema,
            )
        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except asyncio.TimeoutError as exc:
                # A bare TimeoutError stringifies to "", which is how this
                # surfaced in a live run: "plan_story failed:" and nothing else.
                raise TimeoutError(
                    f"{url} did not answer within {self.settings.timeout_sec}s; "
                    "a reasoning model may need a longer llm.timeout_sec"
                ) from exc
        choices = data.get("choices") or []
        if not choices:
            # An OpenAI-compatible server may answer HTTP 200 with an error
            # body: LM Studio does exactly that for an unknown path, so a
            # doubled "/v1" used to surface as an empty completion.
            detail = data.get("error") or data
            raise ValueError(f"{url} returned no choices: {detail!r}"[:500])
        choice = choices[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        if not content and choice.get("finish_reason") == "length":
            # A reasoning model spends max_tokens on `reasoning_content` before
            # it writes any answer. Reporting "could not extract JSON from ''"
            # here sends the reader looking in entirely the wrong place.
            reasoning = len(message.get("reasoning_content") or "")
            raise ValueError(
                f"the model hit max_tokens ({self.settings.max_tokens}) before "
                f"producing any content"
                + (f"; it emitted {reasoning} characters of reasoning first" if reasoning else "")
                + " - raise llm.max_tokens or use a non-reasoning model"
            )
        return content


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
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.absolute_path))
    if errors:
        error = errors[0]
        path = "/" + "/".join(str(part) for part in error.absolute_path)
        raise ValueError(f"{path}: {error.message}")
    return data


def _build_repair_prompt(text: str, schema: dict[str, Any], error: str) -> str:
    """Build the spec 23.4 repair prompt (canonical Japanese)."""

    return (
        "以下のJSONはパースに失敗しました。\n"
        "指定Schemaに合うように修復し、JSONのみを返してください。\n"
        "説明文は不要です。\n\n"
        f"Schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"Previous JSON:\n{text}\n\n"
        f"Validation error: {error}\n"
    )


def enforce_json_schema(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Public helper that wraps :func:`_validate_json` (spec section 23.4)."""

    return _validate_json(text, schema)


# -------------------------------------------------------------- Repair loop
@dataclass
class RepairOutcome:
    """Result of a single ``JSONRepairLoop`` invocation."""

    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    history: list[str] = field(default_factory=list)


@dataclass
class JSONRepairLoop:
    """Configurable retry + repair loop for LLM JSON output (spec 23.4).

    The loop runs the original prompt, then on each failure asks the model to
    repair the previous output via :func:`_build_repair_prompt`.  The number
    of repair attempts is configurable via ``max_repair_attempts``.
    """

    max_repair_attempts: int = 1
    system_prompt: str | None = None

    async def run(
        self,
        provider: LLMProvider,
        prompt: str,
        schema: dict[str, Any],
        *,
        semantic_validator: Callable[[dict[str, Any]], Sequence[str]] | None = None,
    ) -> RepairOutcome:
        attempts = 0
        last_error = "no attempt"
        history: list[str] = []
        text = await provider.complete(prompt, schema=schema, system=self.system_prompt)
        attempts += 1
        history.append(text)
        try:
            data = _validate_json(text, schema)
            _validate_semantics(data, semantic_validator)
            return RepairOutcome(ok=True, data=data, attempts=attempts, history=history)
        except ValueError as exc:
            last_error = str(exc)
            log.warning("LLM JSON invalid (attempt %d): %s", attempts, exc)

        for _ in range(self.max_repair_attempts):
            repair_prompt = _build_repair_prompt(text, schema, last_error)
            text = await provider.complete(
                repair_prompt, schema=schema, system=self.system_prompt
            )
            attempts += 1
            history.append(text)
            try:
                data = _validate_json(text, schema)
                _validate_semantics(data, semantic_validator)
                return RepairOutcome(
                    ok=True,
                    data=data,
                    attempts=attempts,
                    history=history,
                )
            except ValueError as exc:
                last_error = str(exc)
                log.warning("LLM JSON repair invalid (attempt %d): %s", attempts, exc)

        return RepairOutcome(ok=False, error=last_error, attempts=attempts, history=history)


def _validate_semantics(
    data: dict[str, Any],
    validator: Callable[[dict[str, Any]], Sequence[str]] | None,
) -> None:
    if validator is None:
        return
    issues = list(validator(data))
    if issues:
        raise ValueError("semantic validation failed: " + "; ".join(issues))


__all__ = [
    "chat_completions_url",
    "LLMSettings",
    "LLMProvider",
    "ManualProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "build_provider",
    "enforce_json_schema",
    "JSONRepairLoop",
    "RepairOutcome",
    "json_schema_response_format",
]
