from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, ModelSettings, RunConfig, Runner, RunResult, RunResultStreaming
from agents.decorators import tool

pytestmark = pytest.mark.providers


@pytest.mark.parametrize("dictionary", [False, True], ids=["typed", "dictionary"])
async def test_litellm_configured_providers_execute_real_function_tools(
    litellm_models: list[str], dictionary: bool
) -> None:
    from agents.extensions.models.litellm_model import LitellmModel

    calls: list[str] = []

    @tool
    def lookup_package(name: str) -> str:
        """Return the deterministic package health."""
        calls.append(name)
        return "healthy"

    for model_name in litellm_models:
        calls.clear()
        settings: ModelSettings | dict[str, Any]
        values: dict[str, Any] = {"max_tokens": 512}
        settings = values if dictionary else ModelSettings(**values)
        agent = Agent(
            name="Packaged LiteLLM agent",
            model=LitellmModel(model=model_name),
            instructions=(
                "Call lookup_package with name='openai-agents', then reply exactly LITELLM_OK."
            ),
            model_settings=settings,
            tools=[lookup_package],
        )
        result = await Runner.run(
            agent,
            "Check the installed package.",
            run_config=RunConfig(tracing_disabled=True),
        )

        assert calls == ["openai-agents"], model_name
        assert result.final_output == "LITELLM_OK", model_name
        assert result.context_wrapper.usage.total_tokens > 0, model_name


@pytest.mark.filterwarnings(
    "ignore:Accessing the 'model_(computed_)?fields' attribute on the instance is deprecated:"
    "pydantic.warnings.PydanticDeprecatedSince211:"
    r"litellm\.litellm_core_utils\.model_response_utils"
)
async def test_litellm_streaming_preserves_real_provider_usage(integration_model: str) -> None:
    from agents.extensions.models.litellm_model import LitellmModel

    agent = Agent(
        name="Packaged LiteLLM streaming agent",
        model=LitellmModel(model=f"openai/{integration_model}"),
        instructions="Reply with exactly LITELLM_STREAM_OK.",
        model_settings={"max_tokens": 256},
    )
    result = Runner.run_streamed(
        agent,
        "Confirm the streaming provider path.",
        run_config=RunConfig(tracing_disabled=True),
    )
    async for _event in result.stream_events():
        pass

    assert result.final_output == "LITELLM_STREAM_OK"
    assert result.context_wrapper.usage.total_tokens > 0


async def test_litellm_major_external_providers_execute_function_tools(
    external_provider: Any,
) -> None:
    from agents.extensions.models.litellm_model import LitellmModel

    calls: list[str] = []

    @tool
    def provider_status(provider: str) -> str:
        """Return the deterministic provider readiness status."""
        calls.append(provider)
        return "ready"

    agent = Agent(
        name="Packaged LiteLLM external provider agent",
        model=LitellmModel(model=external_provider.model, api_key=external_provider.api_key),
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
    "ignore:Accessing the 'model_(computed_)?fields' attribute on the instance is deprecated:"
    "pydantic.warnings.PydanticDeprecatedSince211:"
    r"litellm\.litellm_core_utils\.model_response_utils"
)
async def test_litellm_external_provider_streams_function_tool_results(
    external_provider: Any,
) -> None:
    from agents.extensions.models.litellm_model import LitellmModel

    calls: list[str] = []

    @tool
    def check_streaming_provider(provider: str) -> str:
        """Return the provider's deterministic streaming readiness."""
        calls.append(provider)
        return "ready"

    agent = Agent(
        name="Packaged LiteLLM streaming external provider agent",
        model=LitellmModel(model=external_provider.model, api_key=external_provider.api_key),
        instructions=(
            "Call check_streaming_provider exactly once with provider='external', "
            "then reply exactly STREAMING_PROVIDER_READY."
        ),
        model_settings={"max_tokens": 512, "include_usage": True},
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


@pytest.mark.filterwarnings(
    "ignore:Accessing the 'model_(computed_)?fields' attribute on the instance is deprecated:"
    "pydantic.warnings.PydanticDeprecatedSince211:"
    r"litellm\.litellm_core_utils\.model_response_utils"
)
@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_litellm_preserves_real_token_logprobs(
    streaming: bool,
) -> None:
    from agents.extensions.models.litellm_model import LitellmModel

    agent = Agent(
        name="Packaged LiteLLM token logprob agent",
        model=LitellmModel(model="openai/gpt-4.1-mini"),
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
