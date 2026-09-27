from __future__ import annotations

from aiohttp import web

from manga_autopilot.services.llm_provider import (
    LLMProvider,
    LLMSettings,
    OpenAICompatibleProvider,
    enforce_json_schema,
    json_schema_response_format,
)


def test_json_schema_response_format_is_strict() -> None:
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }

    result = json_schema_response_format("planning_result", schema)

    assert result == {
        "type": "json_schema",
        "json_schema": {
            "name": "planning_result",
            "strict": True,
            "schema": schema,
        },
    }


async def test_openai_provider_sends_strict_json_schema(aiohttp_server) -> None:
    captured: dict = {}

    async def handle_chat(request: web.Request) -> web.Response:
        captured.update(await request.json())
        return web.json_response(
            {"choices": [{"message": {"content": '{"value": 1}'}}]}
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_chat)
    server = await aiohttp_server(app)
    provider = OpenAICompatibleProvider(
        LLMSettings(
            type="local",
            endpoint=f"http://{server.host}:{server.port}",
            model="qwen3.5-9b",
        )
    )
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }

    await provider.complete("return a value", schema=schema)

    assert captured["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "manga_autopilot_response",
            "strict": True,
            "schema": schema,
        },
    }


def test_schema_validation_rejects_wrong_property_type() -> None:
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }

    try:
        enforce_json_schema('{"value": "one"}', schema)
    except ValueError as exc:
        assert "/value" in str(exc)
    else:
        raise AssertionError("wrong JSON property type was accepted")


class _SequenceProvider(LLMProvider):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(LLMSettings(type="manual"))
        self.responses = list(responses)

    async def complete(self, prompt, *, schema=None, system=None) -> str:
        return self.responses.pop(0)


async def test_semantic_failure_is_retried_once_with_validation_error() -> None:
    provider = _SequenceProvider(['{"value": 1}', '{"value": 2}'])
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }

    result = await provider.complete_json(
        "return two",
        schema,
        max_repair_attempts=1,
        semantic_validator=lambda data: (
            [] if data["value"] == 2 else ["/value: must equal 2"]
        ),
    )

    assert result == {"value": 2}
    assert provider.responses == []
