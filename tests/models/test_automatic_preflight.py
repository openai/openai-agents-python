"""Keep default adapter validation ordering while enforcing opted-in request policies."""

from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI

from agents import Agent, RunConfig, RunHooks, Runner, ToolExecutionConfig, UserError
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("legacy_settings", ["none", "concurrency", "tool-input-hooks"])
async def test_default_chat_validation_preserves_adapter_error_and_llm_hook_order(
    streamed, legacy_settings, monkeypatch
):
    events = []
    config = RunConfig(tracing_disabled=True)
    if legacy_settings == "concurrency":
        config.tool_execution = ToolExecutionConfig(max_function_tool_concurrency=1)
    elif legacy_settings == "tool-input-hooks":

        async def prepare_input(context):
            raise AssertionError("This model response has no tool calls.")

        config.tool_execution = ToolExecutionConfig(
            sdk_only_tool_hooks=True,
            prepare_tool_input=prepare_input,
        )

    class Observer(RunHooks):
        async def on_llm_start(self, context, agent, system_prompt, input_items):
            events.append("llm_start")

    async with AsyncOpenAI(api_key="synthetic-key", base_url="https://api.openai.com/v1") as client:
        sampling = AsyncMock(side_effect=AssertionError("Unexpected sampling"))
        monkeypatch.setattr(client.chat.completions, "create", sampling)
        model = OpenAIChatCompletionsModel("fixture-chat", client, strict_feature_validation=True)
        agent = Agent(name="legacy validation", model=model)
        with pytest.raises(UserError, match="OpenAIChatCompletionsModel does not support"):
            if streamed:
                result = Runner.run_streamed(
                    agent,
                    "start",
                    previous_response_id="existing-response",
                    hooks=Observer(),
                    run_config=config,
                )
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(
                    agent,
                    "start",
                    previous_response_id="existing-response",
                    hooks=Observer(),
                    run_config=config,
                )
        assert events == ["llm_start"]
        sampling.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("policy", ["selector", "sdk-workspace", "native-recovery"])
async def test_opted_in_request_validation_runs_before_hooks_and_sampling(
    streamed, policy, monkeypatch
):
    events = []

    class Observer(RunHooks):
        async def on_llm_start(self, context, agent, system_prompt, input_items):
            events.append("llm_start")

    async with AsyncOpenAI(api_key="synthetic-key", base_url="https://api.openai.com/v1") as client:
        sampling = AsyncMock(side_effect=AssertionError("Unexpected sampling"))
        monkeypatch.setattr(client.chat.completions, "create", sampling)
        model = OpenAIChatCompletionsModel("fixture-chat", client, strict_feature_validation=True)
        config = RunConfig(tracing_disabled=True)
        selections = []
        if policy == "selector":

            def select(turn):
                selections.append(turn.model_name)
                return "fixture-selected"

            config.model_selector = select
            config.model_provider = OpenAIProvider(
                openai_client=client,
                use_responses=False,
                strict_feature_validation=True,
            )
        elif policy == "sdk-workspace":
            config.tool_execution = ToolExecutionConfig(workspace_execution="sdk")
        else:
            config.tool_execution = ToolExecutionConfig(require_native_approval_recovery=True)
        with pytest.raises(UserError, match="server-managed conversation state|CodexProvider"):
            if streamed:
                result = Runner.run_streamed(
                    Agent(
                        name="policy validation",
                        model="fixture-chat" if policy == "selector" else model,
                    ),
                    "start",
                    previous_response_id="existing-response",
                    hooks=Observer(),
                    run_config=config,
                )
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(
                    Agent(
                        name="policy validation",
                        model="fixture-chat" if policy == "selector" else model,
                    ),
                    "start",
                    previous_response_id="existing-response",
                    hooks=Observer(),
                    run_config=config,
                )
        assert events == []
        assert selections == (["fixture-chat"] if policy == "selector" else [])
        sampling.assert_not_called()
