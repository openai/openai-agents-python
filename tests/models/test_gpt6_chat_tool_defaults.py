"""Inspect real Runner request serialization using a framework HTTP mock."""

import json
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from agents import Agent, Model, ModelSettings, OpenAIChatCompletionsModel, RunConfig, Runner
from agents.decorators import tool
from agents.models.openai_provider import OpenAIProvider


@tool
def weather(city: str) -> str:
    """Get weather for a city."""
    return "sunny"


def _chat_completion(model: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "sunny"},
                }
            ],
        },
    )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "entry", "tools", "settings", "responses", "expected"),
    [
        ("gpt-6-sol", "string", "function", ModelSettings(), False, "none"),
        ("gpt-6-luna", "direct", "function", ModelSettings(), False, "none"),
        ("gpt-6-sol", "override", "handoff", ModelSettings(), False, "none"),
        (
            "gpt-6-sol",
            "direct",
            "function",
            ModelSettings(reasoning=Reasoning(effort="high")),
            False,
            "high",
        ),
        (
            "gpt-6-sol",
            "direct",
            "function",
            ModelSettings(extra_args={"reasoning_effort": "low"}),
            False,
            "low",
        ),
        (
            "gpt-6-sol",
            "direct",
            "function",
            ModelSettings(extra_body={"reasoning_effort": "low"}),
            False,
            "low",
        ),
        ("gpt-6-sol", "string", "none", ModelSettings(), False, None),
        ("gpt-6.1-sol", "string", "function", ModelSettings(), False, None),
        ("gpt-6-astra", "string", "function", ModelSettings(), False, None),
        ("gpt-6-sol-unknown", "string", "function", ModelSettings(), False, None),
        ("gpt-6-sol", "string", "function", ModelSettings(), True, None),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_gpt6_tool_request_defaults(
    name: str,
    entry: str,
    tools: str,
    settings: ModelSettings,
    responses: bool,
    expected: str | None,
    stream: bool,
) -> None:
    if responses and stream:
        pytest.skip("Responses preservation is covered by the non-streaming request.")
    before = settings.to_json_dict()
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == ("/v1/responses" if responses else "/v1/chat/completions")
        bodies.append(json.loads(request.content))
        if responses:
            return httpx2.Response(
                200,
                json={
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 0,
                    "model": name,
                    "status": "completed",
                    "output": [],
                    "parallel_tool_calls": False,
                    "tool_choice": "auto",
                    "tools": [],
                },
            )
        if stream:
            chunks = [
                {
                    "id": "chatcmpl_test",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "sunny"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl_test",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                + "data: [DONE]\n\n",
            )
        return _chat_completion(name)

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        provider = OpenAIProvider(openai_client=client, use_responses=responses)
        model = (
            OpenAIChatCompletionsModel(model=name, openai_client=client)
            if entry == "direct"
            else name
        )
        agent = Agent(
            name="test",
            model=model,
            model_settings=settings,
            tools=[weather] if tools == "function" else [],
            handoffs=[Agent(name="other", model=name)] if tools == "handoff" else [],
        )
        config = RunConfig(
            model_provider=provider,
            model=name if entry == "override" else None,
            tracing_disabled=True,
        )
        if stream:
            result = Runner.run_streamed(agent, "Weather?", run_config=config)
            async for _event in result.stream_events():
                pass
        else:
            await Runner.run(agent, "Weather?", run_config=config)
    assert len(bodies) == 1
    body = bodies[0]
    if responses:
        assert "reasoning" not in body
    elif expected is None:
        assert "reasoning_effort" not in body
    else:
        assert body["reasoning_effort"] == expected
    assert bool(body.get("tools")) == (tools != "none")
    assert settings.to_json_dict() == before


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_tool", "override", "expected_effort", "expected_verbosity"),
    [
        pytest.param(True, None, "none", "low", id="tool-default"),
        pytest.param(False, None, "low", "low", id="no-tools"),
        pytest.param(
            True, ModelSettings(reasoning=Reasoning(effort="low")), "low", "low", id="explicit-low"
        ),
        pytest.param(
            True,
            ModelSettings(extra_args={"reasoning_effort": "high", "verbosity": "medium"}),
            "high",
            "medium",
            id="extra-args",
        ),
    ],
)
async def test_routed_chat_tool_defaults(
    with_tool: bool, override: ModelSettings | None, expected_effort: str, expected_verbosity: str
) -> None:
    class RoutedProvider(OpenAIProvider):
        def get_model(self, model_name: str | None) -> Model:
            assert model_name == "gpt-6-astra"
            return super().get_model("gpt-6-sol")

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/chat/completions"
        bodies.append(json.loads(request.content))
        return _chat_completion("gpt-6-sol")

    agent = Agent(name="Weather", model="gpt-6-astra", tools=[weather] if with_tool else [])
    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        provider = RoutedProvider(openai_client=client, use_responses=False)
        config = RunConfig(model_provider=provider, model_settings=override, tracing_disabled=True)
        await Runner.run(agent, "Weather?", run_config=config)

    (body,) = bodies
    assert body["model"] == "gpt-6-sol"
    assert [t["function"]["name"] for t in body.get("tools", [])] == (
        ["weather"] if with_tool else []
    )
    assert body["reasoning_effort"] == expected_effort
    assert body["verbosity"] == expected_verbosity
