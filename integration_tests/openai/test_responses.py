from __future__ import annotations

from typing import Any

import pytest
from openai.resources.responses import AsyncResponses
from pydantic import BaseModel

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
)
from agents.decorators import tool
from agents.items import ToolCallItem, ToolCallOutputItem

pytestmark = pytest.mark.core


class StructuredStatus(BaseModel):
    status: str
    value: int


class NestedStructuredStatus(BaseModel):
    result: StructuredStatus
    note: str | None = None


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_responses_function_tools_preserve_calls_outputs_and_usage(
    integration_model: str, streaming: bool
) -> None:
    called: list[int] = []

    @tool
    def double_number(value: int) -> int:
        """Double the supplied number."""
        called.append(value)
        return value * 2

    agent = Agent(
        name="Packaged Responses tool agent",
        model=integration_model,
        instructions="Call double_number with value 21, then reply exactly RESULT:42.",
        tools=[double_number],
        model_settings=ModelSettings(max_tokens=512),
    )
    config = RunConfig(tracing_disabled=True)
    result: RunResult | RunResultStreaming

    if streaming:
        result = Runner.run_streamed(agent, "Use the tool now.", run_config=config)
        events = [event async for event in result.stream_events()]
        assert any(event.type == "raw_response_event" for event in events)
    else:
        result = await Runner.run(agent, "Use the tool now.", run_config=config)

    assert called == [21]
    assert result.final_output == "RESULT:42"
    assert any(isinstance(item, ToolCallItem) for item in result.new_items)
    assert any(isinstance(item, ToolCallOutputItem) for item in result.new_items)
    assert result.context_wrapper.usage.total_tokens > 0


async def test_responses_structured_output_is_deserialized_from_the_installed_wheel(
    integration_model: str,
) -> None:
    agent = Agent(
        name="Packaged structured output agent",
        model=integration_model,
        instructions="Return status READY and value 42.",
        output_type=StructuredStatus,
        model_settings={"max_tokens": 256},
    )
    result = await Runner.run(
        agent,
        "Return the requested structured result.",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert isinstance(result.final_output, StructuredStatus)
    assert result.final_output.status == "READY"
    assert result.final_output.value == 42


async def test_previous_response_id_preserves_server_managed_conversation(
    integration_model: str,
) -> None:
    agent = Agent(
        name="Packaged server conversation agent",
        model=integration_model,
        model_settings={"max_tokens": 256},
    )
    first = await Runner.run(
        agent,
        "Remember that the secret verification word is ORCHID. Reply only STORED.",
        run_config=RunConfig(tracing_disabled=True),
    )
    assert first.last_response_id is not None

    second = await Runner.run(
        agent,
        "What verification word did I ask you to remember? Reply with only that word.",
        previous_response_id=first.last_response_id,
        run_config=RunConfig(tracing_disabled=True),
    )

    assert second.final_output.strip().upper() == "ORCHID"
    assert second.last_response_id != first.last_response_id


async def test_streaming_structured_output_preserves_nested_optional_fields(
    integration_model: str,
) -> None:
    agent = Agent(
        name="Packaged streamed structured output agent",
        model=integration_model,
        instructions="Return result status READY, result value 42, and note null.",
        output_type=NestedStructuredStatus,
        model_settings={"max_tokens": 384},
    )

    result = Runner.run_streamed(
        agent,
        "Return the nested structured status.",
        run_config=RunConfig(tracing_disabled=True),
    )
    event_types = [event.type async for event in result.stream_events()]

    assert isinstance(result.final_output, NestedStructuredStatus)
    assert result.final_output.result == StructuredStatus(status="READY", value=42)
    assert result.final_output.note is None
    assert "raw_response_event" in event_types


async def test_explicit_prompt_cache_settings_reach_the_live_responses_api(
    integration_model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_requests: list[dict[str, Any]] = []
    original_create = AsyncResponses.create

    async def capture_request(responses: AsyncResponses, *args: Any, **kwargs: Any) -> Any:
        captured_requests.append(kwargs)
        return await original_create(responses, *args, **kwargs)

    monkeypatch.setattr(AsyncResponses, "create", capture_request)
    prefix = " ".join(f"release-checkpoint-{index}" for index in range(1100))
    agent = Agent(
        name="Packaged prompt caching agent",
        model=integration_model,
        instructions="Reply with exactly PROMPT_CACHE_READY.",
        model_settings=ModelSettings(
            max_tokens=128,
            prompt_cache_options={"mode": "explicit", "ttl": "30m"},
            extra_args={"prompt_cache_key": "packaged-integration-explicit-cache"},
        ),
    )
    request_input: list[Any] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": prefix,
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {"type": "input_text", "text": "Reply with PROMPT_CACHE_READY."},
            ],
        }
    ]

    result = await Runner.run(
        agent,
        request_input,
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "PROMPT_CACHE_READY"
    assert result.context_wrapper.usage.input_tokens > 0
    assert len(captured_requests) == 1
    assert captured_requests[0]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert captured_requests[0]["prompt_cache_key"] == "packaged-integration-explicit-cache"
    assert captured_requests[0]["input"][0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }
