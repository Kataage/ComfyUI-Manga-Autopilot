"""Tests for the LLM provider interface (spec section 23)."""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from manga_autopilot.services.llm_provider import (
    LLMSettings,
    ManualProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    build_provider,
    enforce_json_schema,
)


def test_settings_endpoint_required_for_remote() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LLMSettings(type="ollama", model="llama3")
    s = LLMSettings(type="ollama", endpoint="http://localhost:11434", model="llama3")
    assert s.api_key is None


def test_settings_api_key_lookup(monkeypatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "secret-xyz")
    s = LLMSettings(
        type="openai_compatible",
        endpoint="http://localhost:8000",
        model="gpt",
        api_key_env="TEST_LLM_KEY",
    )
    assert s.api_key == "secret-xyz"


def test_build_provider_routes() -> None:
    assert isinstance(build_provider(LLMSettings(type="manual")), ManualProvider)
    assert isinstance(
        build_provider(LLMSettings(type="ollama", endpoint="http://x")),
        OllamaProvider,
    )
    assert isinstance(
        build_provider(LLMSettings(type="openai_compatible", endpoint="http://x")),
        OpenAICompatibleProvider,
    )
    with pytest.raises(ValueError):
        build_provider(LLMSettings(type="nope"))  # type: ignore[arg-type]


async def test_manual_provider_complete() -> None:
    p = ManualProvider(LLMSettings(type="manual"))
    out = await p.complete("hi", schema={"type": "object", "properties": {}})
    assert json.loads(out) == {}


async def test_manual_provider_complete_json() -> None:
    p = ManualProvider(LLMSettings(type="manual"))
    schema = {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    # Manual provider returns "{}" which is missing the required key;
    # the repair attempt should also fail (manual provider does nothing).
    with pytest.raises(ValueError):
        await p.complete_json("prompt", schema)


# ----------------------------------------------------------- aiohttp tests
@pytest.fixture()
async def ollama_server(aiohttp_server):
    async def handle_generate(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(
            {"response": json.dumps({"title": body["prompt"]})}
        )

    app = web.Application()
    app.router.add_post("/api/generate", handle_generate)
    return await aiohttp_server(app)


@pytest.fixture()
async def openai_server(aiohttp_server):
    async def handle_chat(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"title": "OK"})
                        }
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_chat)
    return await aiohttp_server(app)


async def test_ollama_provider_round_trip(ollama_server) -> None:
    settings = LLMSettings(
        type="ollama",
        endpoint=f"http://{ollama_server.host}:{ollama_server.port}",
        model="llama3",
    )
    provider = OllamaProvider(settings)
    out = await provider.complete("hello", schema={"type": "object"})
    assert json.loads(out) == {"title": "hello"}


async def test_ollama_provider_complete_json(ollama_server) -> None:
    settings = LLMSettings(
        type="ollama",
        endpoint=f"http://{ollama_server.host}:{ollama_server.port}",
        model="llama3",
    )
    provider = OllamaProvider(settings)
    schema = {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    result = await provider.complete_json("Title please", schema=schema)
    assert result == {"title": "Title please"}


async def test_openai_compatible_provider(openai_server) -> None:
    settings = LLMSettings(
        type="openai_compatible",
        endpoint=f"http://{openai_server.host}:{openai_server.port}",
        model="gpt",
    )
    provider = OpenAICompatibleProvider(settings)
    schema = {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    result = await provider.complete_json("hi", schema=schema)
    assert result == {"title": "OK"}


def test_enforce_json_schema_strips_fence() -> None:
    text = "Here you go:\n```json\n{\"a\": 1}\n```\nDone."
    assert enforce_json_schema(text, {"type": "object", "required": ["a"]}) == {"a": 1}


def test_enforce_json_schema_missing_key() -> None:
    with pytest.raises(ValueError):
        enforce_json_schema(
            "{\"a\": 1}", {"type": "object", "required": ["a", "b"]}
        )


def test_enforce_json_schema_non_dict() -> None:
    with pytest.raises(ValueError):
        enforce_json_schema("[1, 2, 3]", {"type": "object"})


def test_enforce_json_schema_unparseable() -> None:
    with pytest.raises(ValueError):
        enforce_json_schema("not json at all", {"type": "object"})
