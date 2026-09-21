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


# ------------------------------------------------- endpoint and error surfacing
#
# Found live on 2026-08-27: configuring the endpoint as ".../v1" produced
# ".../v1/v1/chat/completions", and LM Studio answers an unknown path with
# HTTP 200 and an {"error": ...} body. raise_for_status() saw nothing wrong and
# the provider returned "", which surfaced several layers up as the useless
# "could not extract JSON from LLM response: ''".


def test_chat_completions_url_tolerates_a_v1_suffix() -> None:
    from manga_autopilot.services.llm_provider import chat_completions_url

    expected = "http://127.0.0.1:1234/v1/chat/completions"
    for endpoint in (
        "http://127.0.0.1:1234",
        "http://127.0.0.1:1234/",
        "http://127.0.0.1:1234/v1",
        "http://127.0.0.1:1234/v1/",
    ):
        assert chat_completions_url(endpoint) == expected


def test_chat_completions_url_keeps_a_base_path() -> None:
    from manga_autopilot.services.llm_provider import chat_completions_url

    assert (
        chat_completions_url("https://gateway.example/llm")
        == "https://gateway.example/llm/v1/chat/completions"
    )


async def test_a_200_error_body_is_reported_not_swallowed(aiohttp_client) -> None:
    """An OpenAI-compatible server can answer 200 with an error body."""
    from aiohttp import web

    from manga_autopilot.services.llm_provider import LLMSettings, OpenAICompatibleProvider

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": "Unexpected endpoint or method."})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    client = await aiohttp_client(app)
    endpoint = f"http://127.0.0.1:{client.port}"

    provider = OpenAICompatibleProvider(
        LLMSettings(type="openai_compatible", endpoint=endpoint, model="m")
    )

    with pytest.raises(ValueError, match="no choices"):
        await provider.complete("hello")


async def test_an_empty_reasoning_response_names_the_token_budget(aiohttp_client) -> None:
    from aiohttp import web

    from manga_autopilot.services.llm_provider import LLMSettings, OpenAICompatibleProvider

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "x" * 900},
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    client = await aiohttp_client(app)

    provider = OpenAICompatibleProvider(
        LLMSettings(
            type="openai_compatible",
            endpoint=f"http://127.0.0.1:{client.port}",
            model="m",
            max_tokens=256,
        )
    )

    with pytest.raises(ValueError, match="max_tokens"):
        await provider.complete("hello")


async def test_a_timeout_says_so_instead_of_raising_an_empty_error(aiohttp_client) -> None:
    """A live run reported 'plan_story failed:' with nothing after the colon.

    aiohttp's default 300s total timeout expired on a slow local reasoning
    model, and a bare TimeoutError stringifies to the empty string.
    """
    import asyncio as _asyncio

    from aiohttp import web

    from manga_autopilot.services.llm_provider import LLMSettings, OpenAICompatibleProvider

    async def handler(request: web.Request) -> web.Response:
        await _asyncio.sleep(5)
        return web.json_response({"choices": []})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    client = await aiohttp_client(app)

    provider = OpenAICompatibleProvider(
        LLMSettings(
            type="openai_compatible",
            endpoint=f"http://127.0.0.1:{client.port}",
            model="m",
            timeout_sec=1,
        )
    )

    with pytest.raises(TimeoutError) as excinfo:
        await provider.complete("hello")

    assert str(excinfo.value), "the error must not be empty"
    assert "1s" in str(excinfo.value)


def test_the_default_timeout_clears_aiohttps_own(aiohttp_client) -> None:
    from manga_autopilot.services.llm_provider import LLMSettings

    assert LLMSettings().timeout_sec > 300


# ------------------------------------------------------------- planner cost
#
# Planner latency dominates a strict run: the same two-page plan measured
# 66.8s, 230.9s, 420.4s, 651.2s and over 900s. Most of that is chain of thought
# that is discarded, and nothing recorded it, so the only symptom was a run
# that sometimes took half an hour.


def test_stats_start_empty() -> None:
    from manga_autopilot.services.llm_provider import CompletionStats

    stats = CompletionStats()

    assert stats.calls == 0
    assert stats.reasoning_ratio == 0.0


def test_stats_accumulate_across_calls() -> None:
    from manga_autopilot.services.llm_provider import CompletionStats

    stats = CompletionStats()
    stats.record(seconds=296.7, content="x" * 393, reasoning="y" * 14021)
    stats.record(seconds=43.1, content="x" * 861, reasoning="y" * 1343)

    assert stats.calls == 2
    assert round(stats.seconds, 1) == 339.8
    assert stats.reasoning_chars == 15364
    assert stats.content_chars == 1254


def test_reasoning_ratio_shows_how_much_was_discarded() -> None:
    from manga_autopilot.services.llm_provider import CompletionStats

    stats = CompletionStats()
    stats.record(seconds=1.0, content="x" * 100, reasoning="y" * 900)

    assert stats.reasoning_ratio == 0.9
    assert stats.to_dict()["reasoning_ratio"] == 0.9


def test_a_model_that_does_not_reason_has_a_zero_ratio() -> None:
    from manga_autopilot.services.llm_provider import CompletionStats

    stats = CompletionStats()
    stats.record(seconds=3.9, content="x" * 12)

    assert stats.reasoning_ratio == 0.0
    assert stats.reasoning_chars == 0


async def test_a_provider_records_what_each_completion_cost(aiohttp_client) -> None:
    import asyncio

    from aiohttp import web

    from manga_autopilot.services.llm_provider import LLMSettings, OpenAICompatibleProvider

    async def handler(request: web.Request) -> web.Response:
        # A loopback reply can land inside the clock's resolution, so make the
        # elapsed time observable.
        await asyncio.sleep(0.05)
        return web.json_response(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"ok": true}',
                            "reasoning_content": "z" * 500,
                        },
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    client = await aiohttp_client(app)
    provider = OpenAICompatibleProvider(
        LLMSettings(
            type="openai_compatible",
            endpoint=f"http://127.0.0.1:{client.port}",
            model="m",
        )
    )

    await provider.complete("hello")
    await provider.complete("hello again")

    assert provider.stats.calls == 2
    assert provider.stats.reasoning_chars == 1000
    assert provider.stats.content_chars == 24
    assert provider.stats.seconds > 0


async def test_the_provider_logs_the_reasoning_length(
    aiohttp_client, caplog
) -> None:
    """The 297s / 14,021-character behaviour was invisible until it was logged."""
    import logging

    from aiohttp import web

    from manga_autopilot.services.llm_provider import LLMSettings, OpenAICompatibleProvider

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"choices": [{"finish_reason": "stop", "message": {"content": "ok", "reasoning_content": "z" * 700}}]}
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    client = await aiohttp_client(app)
    provider = OpenAICompatibleProvider(
        LLMSettings(
            type="openai_compatible",
            endpoint=f"http://127.0.0.1:{client.port}",
            model="slow-reasoner",
        )
    )

    with caplog.at_level(logging.INFO):
        await provider.complete("hello")

    assert "slow-reasoner" in caplog.text
    assert "700 of reasoning" in caplog.text


def test_the_snapshot_can_carry_the_planner_cost() -> None:
    from manga_autopilot.services.llm_provider import CompletionStats
    from manga_autopilot.services.run_snapshot import PlannerCostSnapshot, RunSnapshot

    stats = CompletionStats()
    stats.record(seconds=296.7, content="x" * 393, reasoning="y" * 14021)

    snapshot = RunSnapshot(
        run_id="r1",
        project_id="p1",
        planner=PlannerCostSnapshot(**stats.to_dict()),
    )

    assert snapshot.planner.calls == 1
    assert snapshot.planner.reasoning_ratio > 0.9
    assert "reasoning_ratio" in snapshot.model_dump(mode="json")["planner"]
