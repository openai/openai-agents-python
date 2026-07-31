from __future__ import annotations

from typing import Any

import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from agents import Agent, RunConfig, Runner, RunResult, RunResultStreaming
from agents.decorators import tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

pytestmark = pytest.mark.core


class ChatCompletionStatus(BaseModel):
    status: str
    checkpoints: list[int]


@pytest.mark.parametrize("dictionary", [False, True], ids=["typed", "dictionary"])
@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_chat_completions_tools_settings_and_usage(
    integration_model: str, dictionary: bool, streaming: bool
) -> None:
    from agents import ModelSettings

    calls: list[str] = []

    @tool
    def package_status(package: str) -> str:
        """Return a deterministic package status."""
        calls.append(package)
        return "ready"

    values: dict[str, Any] = {
        "reasoning": {"effort": "none"},
        "include_usage": True,
        "extra_args": {"max_completion_tokens": 512},
    }
    settings = values if dictionary else ModelSettings(**values)
    agent = Agent(
        name="Packaged Chat Completions agent",
        model=OpenAIChatCompletionsModel(
            model=integration_model,
            openai_client=AsyncOpenAI(),
        ),
        instructions=(
            "Call package_status exactly once with package='openai-agents', then reply "
            "exactly CHAT_READY."
        ),
        model_settings=settings,
        tools=[package_status],
    )
    config = RunConfig(tracing_disabled=True)
    result: RunResult | RunResultStreaming

    if streaming:
        result = Runner.run_streamed(agent, "Check the package.", run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "Check the package.", run_config=config)

    assert calls == ["openai-agents"]
    assert result.final_output == "CHAT_READY"
    assert result.context_wrapper.usage.total_tokens > 0


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_chat_completions_preserves_typed_structured_output(
    integration_model: str,
    streaming: bool,
) -> None:
    agent = Agent(
        name="Packaged structured Chat Completions agent",
        model=OpenAIChatCompletionsModel(
            model=integration_model,
            openai_client=AsyncOpenAI(),
        ),
        instructions="Return status CHAT_STRUCTURED_READY and checkpoints [2, 4, 8].",
        output_type=ChatCompletionStatus,
        model_settings={"reasoning": {"effort": "none"}, "include_usage": True},
    )
    result: RunResult | RunResultStreaming
    if streaming:
        result = Runner.run_streamed(
            agent,
            "Return the requested typed release status.",
            run_config=RunConfig(tracing_disabled=True),
        )
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(
            agent,
            "Return the requested typed release status.",
            run_config=RunConfig(tracing_disabled=True),
        )

    assert result.final_output == ChatCompletionStatus(
        status="CHAT_STRUCTURED_READY", checkpoints=[2, 4, 8]
    )
    assert result.context_wrapper.usage.total_tokens > 0
