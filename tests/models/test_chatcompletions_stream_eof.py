"""Exercise terminal detection through the production OpenAI SSE parser and Runner."""

import json
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.decorators import tool
from agents.exceptions import ModelBehaviorError
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

pytestmark = [pytest.mark.asyncio, pytest.mark.allow_call_model_methods]


def _sse_body(delta: dict[str, Any] | None, finish_reason: str | None) -> bytes:
    # Mock only the HTTP transport: parsing and adapter finalization stay real.
    chunks = []
    if delta is not None:
        chunks.append({"index": 0, "delta": delta, "finish_reason": None})
    if finish_reason is not None:
        chunks.append({"index": 0, "delta": {}, "finish_reason": finish_reason})
    payloads = [
        {
            "id": "chatcmpl-synthetic",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o-mini",
            "choices": [choice],
        }
        for choice in chunks
    ]
    if finish_reason is not None:
        payloads.append(
            {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-4o-mini",
                "choices": [],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        )
    body = "".join(f"data: {json.dumps(payload)}\n\n" for payload in payloads)
    return body.encode()


@pytest.mark.parametrize("buffered", [False, True])
@pytest.mark.parametrize("output", ["text", "tool", "empty"])
async def test_official_stream_rejects_eof_before_terminal_choice(
    buffered: bool, output: str
) -> None:
    calls = []

    @tool
    def lookup() -> str:
        calls.append("lookup")
        return "result"

    delta = {
        "text": {"content": "The answer is "},
        "tool": {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-synthetic",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ]
        },
        "empty": None,
    }[output]
    http_response = httpx2.Response(
        200, content=_sse_body(delta, None), headers={"content-type": "text/event-stream"}
    )
    async with AsyncOpenAI(
        api_key="synthetic",
        base_url="https://api.openai.com/v1",
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda request: http_response), trust_env=False
        ),
    ) as client:
        agent = Agent(
            name="Synthetic",
            tools=[lookup],
            model=OpenAIChatCompletionsModel(
                "gpt-4o-mini", client, buffer_streamed_tool_calls=buffered
            ),
        )
        result = Runner.run_streamed(agent, "Answer", run_config=RunConfig(tracing_disabled=True))
        raw_types = []
        with pytest.raises(ModelBehaviorError, match="before receiving a finish_reason"):
            async for event in result.stream_events():
                if event.type == "raw_response_event":
                    raw_types.append(event.data.type)

    assert "response.completed" not in raw_types
    assert "response.output_item.done" not in raw_types
    assert result.final_output is None
    assert calls == []
    assert http_response.is_closed


@pytest.mark.parametrize("buffered", [False, True])
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
async def test_official_stream_accepts_terminal_choice_with_usage_trailer(
    buffered: bool, finish_reason: str
) -> None:
    body = _sse_body({"content": "Answer"}, finish_reason) + b"data: [DONE]\n\n"
    async with AsyncOpenAI(
        api_key="synthetic",
        base_url="https://api.openai.com/v1",
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, content=body, headers={"content-type": "text/event-stream"}
                )
            ),
            trust_env=False,
        ),
    ) as client:
        result = Runner.run_streamed(
            Agent(
                name="Synthetic",
                model=OpenAIChatCompletionsModel(
                    "gpt-4o-mini", client, buffer_streamed_tool_calls=buffered
                ),
                model_settings=ModelSettings(preserve_raw_usage=True),
            ),
            "Answer",
            run_config=RunConfig(tracing_disabled=True),
        )
        events = [event async for event in result.stream_events()]

    assert result.final_output == "Answer"
    assert (
        sum(
            event.type == "raw_response_event" and event.data.type == "response.completed"
            for event in events
        )
        == 1
    )
    assert result.context_wrapper.usage.total_tokens == 5


@pytest.mark.parametrize("buffered", [False, True])
async def test_third_party_stream_preserves_eof_completion(buffered: bool) -> None:
    body = _sse_body({"content": "Answer"}, None)
    async with AsyncOpenAI(
        api_key="synthetic",
        base_url="https://provider.example/v1",
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, content=body, headers={"content-type": "text/event-stream"}
                )
            ),
            trust_env=False,
        ),
    ) as client:
        result = Runner.run_streamed(
            Agent(
                name="Synthetic",
                model=OpenAIChatCompletionsModel(
                    "provider-model", client, buffer_streamed_tool_calls=buffered
                ),
            ),
            "Answer",
            run_config=RunConfig(tracing_disabled=True),
        )
        async for _ in result.stream_events():
            pass

    assert result.final_output == "Answer"


@pytest.mark.parametrize("buffered", [False, True])
async def test_official_stream_executes_tool_after_terminal_choice(buffered: bool) -> None:
    calls = []

    @tool
    def lookup() -> str:
        calls.append("lookup")
        return "result"

    body = (
        _sse_body(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-synthetic",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ]
            },
            "tool_calls",
        )
        + b"data: [DONE]\n\n"
    )
    async with AsyncOpenAI(
        api_key="synthetic",
        base_url="https://api.openai.com/v1",
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, content=body, headers={"content-type": "text/event-stream"}
                )
            ),
            trust_env=False,
        ),
    ) as client:
        result = Runner.run_streamed(
            Agent(
                name="Synthetic",
                tools=[lookup],
                tool_use_behavior="stop_on_first_tool",
                model=OpenAIChatCompletionsModel(
                    "gpt-4o-mini", client, buffer_streamed_tool_calls=buffered
                ),
            ),
            "Answer",
            run_config=RunConfig(tracing_disabled=True),
        )
        async for _ in result.stream_events():
            pass

    assert calls == ["lookup"]
    assert result.final_output == "result"
    assert result.context_wrapper.usage.total_tokens == 5
