from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, ModelSettings, RunConfig, Runner, RunResult, RunResultStreaming
from agents.decorators import tool

pytestmark = pytest.mark.providers


@pytest.mark.parametrize("dictionary", [False, True], ids=["typed", "dictionary"])
async def test_any_llm_configured_providers_execute_real_function_tools(
    any_llm_models: list[str], dictionary: bool
) -> None:
    from agents.extensions.models.any_llm_model import AnyLLMModel

    calls: list[int] = []

    @tool
    def lookup_number(value: int) -> int:
        """Return the supplied deterministic number."""
        calls.append(value)
        return value

    for model_name in any_llm_models:
        calls.clear()
        settings: ModelSettings | dict[str, Any]
        settings = {"max_tokens": 512} if dictionary else ModelSettings(max_tokens=512)
        agent = Agent(
            name="Packaged AnyLLM agent",
            model=AnyLLMModel(model=model_name),
            instructions="Call lookup_number with 42 and then reply exactly ANY_LLM:42.",
            model_settings=settings,
            tools=[lookup_number],
        )
        result = await Runner.run(
            agent,
            "Use the number tool.",
            run_config=RunConfig(tracing_disabled=True),
        )

        assert calls == [42], model_name
        assert result.final_output == "ANY_LLM:42", model_name
        assert result.context_wrapper.usage.total_tokens > 0, model_name


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
async def test_any_llm_openai_supports_both_api_families(integration_model: str, api: str) -> None:
    from agents.extensions.models.any_llm_model import AnyLLMModel

    agent = Agent(
        name="Packaged AnyLLM API selector",
        model=AnyLLMModel(model=f"openai/{integration_model}", api=api),  # type: ignore[arg-type]
        instructions="Reply with exactly ANY_LLM_API_OK.",
        model_settings={"max_tokens": 256},
    )
    result = await Runner.run(
        agent,
        "Confirm the selected provider API.",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "ANY_LLM_API_OK"


@pytest.mark.filterwarnings(
    "ignore:Inheritance class AiohttpClientSession from ClientSession is discouraged:"
    r"DeprecationWarning:google\.genai\._api_client"
)
async def test_any_llm_major_external_providers_execute_function_tools(
    external_provider: Any,
) -> None:
    from agents.extensions.models.any_llm_model import AnyLLMModel

    calls: list[str] = []

    @tool
    def provider_status(provider: str) -> str:
        """Return the deterministic provider readiness status."""
        calls.append(provider)
        return "ready"

    agent = Agent(
        name="Packaged AnyLLM external provider agent",
        model=AnyLLMModel(model=external_provider.model, api_key=external_provider.api_key),
        instructions=(
            "Call provider_status exactly once with provider='external', "
            "then reply exactly PROVIDER_READY."
        ),
        model_settings={"max_tokens": 512},
        tools=[provider_status],
    )
    result = await Runner.run(
        agent,
        "Check the provider with its function tool.",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert calls == ["external"]
    assert result.final_output == "PROVIDER_READY"
    assert result.context_wrapper.usage.total_tokens > 0


@pytest.mark.nightly
@pytest.mark.filterwarnings(
    "ignore:Inheritance class AiohttpClientSession from ClientSession is discouraged:"
    r"DeprecationWarning:google\.genai\._api_client"
)
async def test_any_llm_external_provider_streams_function_tool_results(
    external_provider: Any,
) -> None:
    from agents.extensions.models.any_llm_model import AnyLLMModel

    calls: list[str] = []

    @tool
    def check_streaming_provider(provider: str) -> str:
        """Return the provider's deterministic streaming readiness."""
        calls.append(provider)
        return "ready"

    agent = Agent(
        name="Packaged AnyLLM streaming external provider agent",
        model=AnyLLMModel(model=external_provider.model, api_key=external_provider.api_key),
        instructions=(
            "Call check_streaming_provider exactly once with provider='external', "
            "then reply exactly STREAMING_PROVIDER_READY."
        ),
        model_settings={"max_tokens": 512},
        tools=[check_streaming_provider],
    )
    result = Runner.run_streamed(
        agent,
        "Check the streamed external provider function tool.",
        run_config=RunConfig(tracing_disabled=True),
    )
    event_types = [event.type async for event in result.stream_events()]

    assert calls == ["external"]
    assert result.final_output == "STREAMING_PROVIDER_READY"
    assert "raw_response_event" in event_types
    assert result.context_wrapper.usage.total_tokens > 0


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_any_llm_chat_completions_preserves_real_token_logprobs(
    streaming: bool,
) -> None:
    from agents.extensions.models.any_llm_model import AnyLLMModel

    agent = Agent(
        name="Packaged AnyLLM token logprob agent",
        model=AnyLLMModel(model="openai/gpt-4.1-mini", api="chat_completions"),
        instructions="Reply with exactly BLUE.",
        model_settings=ModelSettings(top_logprobs=2, max_tokens=32),
    )
    config = RunConfig(tracing_disabled=True)
    result: RunResult | RunResultStreaming
    if streaming:
        result = Runner.run_streamed(agent, "What color is the sky? Reply BLUE.", run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "What color is the sky? Reply BLUE.", run_config=config)

    texts = [
        content
        for response in result.raw_responses
        for item in response.output
        for content in getattr(item, "content", [])
        if getattr(content, "type", None) == "output_text"
    ]
    assert result.final_output == "BLUE"
    assert texts
    assert any(getattr(content, "logprobs", None) for content in texts)
