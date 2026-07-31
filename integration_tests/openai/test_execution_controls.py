from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from openai.types.responses import ResponseReasoningItem

from agents import (
    Agent,
    AgentHookContext,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    RunHooks,
    Runner,
    RunResult,
    RunResultStreaming,
    SQLiteSession,
    Tool,
    ToolCallOutputItem,
    ToolExecutionConfig,
)
from agents.decorators import tool
from agents.items import ModelResponse, TResponseInputItem
from agents.run_config import CallModelData, ModelInputData

pytestmark = pytest.mark.core


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_stop_on_first_tool_avoids_a_follow_up_model_request(
    integration_model: str,
    streaming: bool,
) -> None:
    calls: list[str] = []

    @tool
    def resolve_checkpoint(checkpoint: str) -> str:
        """Return the requested release checkpoint directly."""
        calls.append(checkpoint)
        return "STOP_ON_FIRST_TOOL_READY"

    agent = Agent(
        name="Packaged stop-on-tool agent",
        model=integration_model,
        instructions="Call resolve_checkpoint exactly once with checkpoint='release'.",
        tools=[resolve_checkpoint],
        tool_use_behavior="stop_on_first_tool",
        model_settings={"max_tokens": 256, "tool_choice": "required"},
    )
    result: RunResult | RunResultStreaming
    if streaming:
        result = Runner.run_streamed(
            agent,
            "Return the checkpoint through the tool.",
            run_config=RunConfig(tracing_disabled=True),
        )
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(
            agent,
            "Return the checkpoint through the tool.",
            run_config=RunConfig(tracing_disabled=True),
        )

    assert calls == ["release"]
    assert result.final_output == "STOP_ON_FIRST_TOOL_READY"
    assert result.context_wrapper.usage.requests == 1
    assert len(result.raw_responses) == 1


@pytest.mark.nightly
@pytest.mark.parametrize("max_concurrency", [1, 2], ids=["sequential", "bounded-parallel"])
@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_parallel_function_tools_preserve_order_and_sdk_concurrency_limits(
    integration_model: str,
    max_concurrency: int,
    streaming: bool,
) -> None:
    active_calls = 0
    peak_concurrency = 0

    async def run_checkpoint(name: str) -> str:
        nonlocal active_calls, peak_concurrency
        active_calls += 1
        peak_concurrency = max(peak_concurrency, active_calls)
        try:
            await asyncio.sleep(0.08)
            return name.upper()
        finally:
            active_calls -= 1

    @tool
    async def checkpoint_alpha(checkpoint: str) -> str:
        """Return the alpha release checkpoint."""
        return await run_checkpoint(checkpoint)

    @tool
    async def checkpoint_beta(checkpoint: str) -> str:
        """Return the beta release checkpoint."""
        return await run_checkpoint(checkpoint)

    settings = ModelSettings(
        tool_choice="required",
        parallel_tool_calls=True,
        max_tokens=512,
    )
    agent = Agent(
        name="Packaged bounded tool concurrency agent",
        model=integration_model,
        instructions=(
            "In the same turn, call checkpoint_alpha with checkpoint='alpha' and "
            "checkpoint_beta with checkpoint='beta'. After both tools finish reply exactly "
            "CONCURRENCY_READY."
        ),
        tools=[checkpoint_alpha, checkpoint_beta],
        model_settings=settings,
    )
    config = RunConfig(
        tracing_disabled=True,
        tool_execution=ToolExecutionConfig(max_function_tool_concurrency=max_concurrency),
    )
    result: RunResult | RunResultStreaming
    if streaming:
        result = Runner.run_streamed(agent, "Run both release checkpoints.", run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "Run both release checkpoints.", run_config=config)

    outputs = [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)]

    assert result.final_output == "CONCURRENCY_READY"
    assert outputs == ["ALPHA", "BETA"]
    assert peak_concurrency == max_concurrency
    assert result.context_wrapper.usage.requests == 2
    assert settings.tool_choice == "required"


async def test_session_merge_and_model_input_filter_have_distinct_persistence_boundaries(
    integration_model: str,
    tmp_path: Path,
) -> None:
    callback_inputs: list[tuple[int, str]] = []
    filter_inputs: list[str] = []
    agent = Agent(
        name="Packaged session input filtering agent",
        model=integration_model,
        instructions="Remember user-provided release words and reply exactly as requested.",
        model_settings={"max_tokens": 256},
    )
    session = SQLiteSession("packaged-filtered-session", tmp_path / "filtered.sqlite3")

    try:
        await Runner.run(
            agent,
            "Remember the release word JASPER and reply only STORED.",
            session=session,
            run_config=RunConfig(tracing_disabled=True),
        )

        def merge_session_input(
            history: list[TResponseInputItem],
            new_input: list[TResponseInputItem],
        ) -> list[TResponseInputItem]:
            callback_inputs.append((len(history), str(new_input[0].get("content"))))
            rewritten = cast(
                TResponseInputItem,
                {
                    "role": "user",
                    "content": "What release word did I provide? Reply only with that word.",
                },
            )
            return [*history, rewritten]

        def filter_model_input(data: CallModelData[Any]) -> ModelInputData:
            latest = data.model_data.input[-1]
            filter_inputs.append(str(latest.get("content")))
            return ModelInputData(
                input=data.model_data.input,
                instructions=(data.model_data.instructions or "")
                + " Prefix the remembered word with FILTERED: and reply with nothing else.",
            )

        result = await Runner.run(
            agent,
            "PLACEHOLDER_NEW_INPUT",
            session=session,
            run_config=RunConfig(
                tracing_disabled=True,
                session_input_callback=merge_session_input,
                call_model_input_filter=filter_model_input,
            ),
        )
        persisted = await session.get_items()
    finally:
        session.close()

    assert len(callback_inputs) == 1
    assert callback_inputs[0][0] >= 2
    assert callback_inputs[0][1] == "PLACEHOLDER_NEW_INPUT"
    assert filter_inputs == ["What release word did I provide? Reply only with that word."]
    assert result.final_output == "FILTERED:JASPER"
    assert any("What release word" in str(item.get("content", "")) for item in persisted)
    assert not any("PLACEHOLDER_NEW_INPUT" in str(item.get("content", "")) for item in persisted)


@pytest.mark.nightly
@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_stateless_reasoning_replay_preserves_encrypted_content_when_returned(
    integration_model: str,
    streaming: bool,
) -> None:
    stored_words: list[str] = []

    @tool
    def remember_word(word: str) -> str:
        """Store the release word and return a deterministic acknowledgement."""
        stored_words.append(word)
        return "WORD_STORED"

    agent = Agent(
        name="Packaged stateless reasoning replay agent",
        model=integration_model,
        instructions=(
            "Calculate the requested arithmetic before calling remember_word. Use the word "
            "AMBER when the result is odd and COBALT when it is even. Once the tool succeeds "
            "reply exactly STORED. Answer follow-up questions using the previous tool result."
        ),
        tools=[remember_word],
        model_settings=ModelSettings(
            store=False,
            reasoning={"effort": "medium", "summary": "auto"},
            response_include=["reasoning.encrypted_content"],
            max_tokens=1024,
        ),
    )
    config = RunConfig(tracing_disabled=True, reasoning_item_id_policy="omit")
    first = await Runner.run(
        agent,
        "What is the remainder when 4837 multiplied by 8291 is divided by 97? "
        "Follow the parity rule, call remember_word, and then reply only STORED.",
        run_config=config,
    )
    reasoning_items = [
        item
        for response in first.raw_responses
        for item in response.output
        if isinstance(item, ResponseReasoningItem)
    ]
    assert reasoning_items, "The stateless response did not contain any reasoning items."
    replay = first.to_input_list(mode="normalized")
    replayed_reasoning = [item for item in replay if item.get("type") == "reasoning"]
    replay.append(
        cast(
            TResponseInputItem,
            {"role": "user", "content": "What release word did I provide? Reply only AMBER."},
        )
    )

    result: RunResult | RunResultStreaming
    if streaming:
        result = Runner.run_streamed(agent, replay, run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, replay, run_config=config)

    assert all(isinstance(item.encrypted_content, str) for item in reasoning_items)
    assert len(replayed_reasoning) == len(reasoning_items)
    assert all(isinstance(item.get("encrypted_content"), str) for item in replayed_reasoning)
    assert all("id" not in item for item in replayed_reasoning)
    assert first.context_wrapper.usage.requests == 2
    assert result.context_wrapper.usage.requests == 1
    assert stored_words == ["AMBER"]
    assert result.final_output == "AMBER"


@pytest.mark.parametrize("use_session", [False, True], ids=["without-session", "sqlite-session"])
async def test_cancel_after_turn_resumes_without_repeating_function_tools(
    integration_model: str,
    use_session: bool,
) -> None:
    calls: list[str] = []

    @tool
    def checkpoint(value: str) -> str:
        """Record one deterministic cancellation checkpoint."""
        calls.append(value)
        return "CANCEL_CHECKPOINT_READY"

    session = SQLiteSession("packaged-cancel-after-turn") if use_session else None
    agent = Agent(
        name="Packaged streamed cancellation agent",
        model=integration_model,
        instructions=(
            "Call checkpoint with value='release'. After the tool returns, reply exactly "
            "CANCEL_RESUMED_READY."
        ),
        tools=[checkpoint],
        model_settings={"max_tokens": 256},
    )
    config = RunConfig(tracing_disabled=True)

    try:
        result = Runner.run_streamed(
            agent,
            "Run the release checkpoint.",
            session=session,
            run_config=config,
        )
        async for event in result.stream_events():
            if getattr(event, "name", None) == "tool_called":
                result.cancel(mode="after_turn")

        replay = result.to_input_list(mode="normalized")
        resumed = await Runner.run(result.last_agent, replay, run_config=config)
        persisted = await session.get_items() if session is not None else []
    finally:
        if session is not None:
            session.close()

    assert result.final_output is None
    assert result.is_complete
    assert calls == ["release"]
    assert resumed.final_output == "CANCEL_RESUMED_READY"
    assert result.context_wrapper.usage.requests == 1
    assert resumed.context_wrapper.usage.requests == 1
    if use_session:
        assert any(item.get("type") == "function_call_output" for item in persisted)


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_max_turn_error_handler_preserves_tool_side_effects_and_history(
    integration_model: str,
    streaming: bool,
) -> None:
    calls: list[str] = []
    handled: list[str] = []

    @tool
    def release_checkpoint(value: str) -> str:
        """Return a release checkpoint before the model exceeds its turn limit."""
        calls.append(value)
        return "CHECKPOINT_RECORDED"

    def handle_max_turns(data: RunErrorHandlerInput[Any]) -> RunErrorHandlerResult:
        handled.append(type(data.error).__name__)
        return RunErrorHandlerResult(final_output="MAX_TURNS_RECOVERED", include_in_history=False)

    session = SQLiteSession(f"packaged-max-turn-recovery-{streaming}")
    agent = Agent(
        name="Packaged max-turn recovery agent",
        model=integration_model,
        instructions="Call release_checkpoint with value='release', then explain the result.",
        tools=[release_checkpoint],
        model_settings={"tool_choice": "required", "max_tokens": 256},
    )
    config = RunConfig(tracing_disabled=True)

    try:
        result: RunResult | RunResultStreaming
        if streaming:
            result = Runner.run_streamed(
                agent,
                "Run the release checkpoint.",
                session=session,
                run_config=config,
                max_turns=1,
                error_handlers={"max_turns": handle_max_turns},
            )
            async for _event in result.stream_events():
                pass
        else:
            result = await Runner.run(
                agent,
                "Run the release checkpoint.",
                session=session,
                run_config=config,
                max_turns=1,
                error_handlers={"max_turns": handle_max_turns},
            )
        persisted = await session.get_items()
    finally:
        session.close()

    assert calls == ["release"]
    assert handled == ["MaxTurnsExceeded"]
    assert result.final_output == "MAX_TURNS_RECOVERED"
    assert any(item.get("type") == "function_call_output" for item in persisted)
    assert not any("MAX_TURNS_RECOVERED" in str(item) for item in persisted)


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_live_run_hooks_preserve_model_and_function_tool_event_order(
    integration_model: str,
    streaming: bool,
) -> None:
    observed: list[str] = []

    class RecordingHooks(RunHooks[Any]):
        async def on_agent_start(self, context: AgentHookContext[Any], agent: Agent[Any]) -> None:
            del context, agent
            observed.append("agent_start")

        async def on_agent_end(
            self,
            context: AgentHookContext[Any],
            agent: Agent[Any],
            output: Any,
        ) -> None:
            del context, agent, output
            observed.append("agent_end")

        async def on_llm_start(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            system_prompt: str | None,
            input_items: list[TResponseInputItem],
        ) -> None:
            del context, agent, system_prompt, input_items
            observed.append("model_start")

        async def on_llm_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            response: ModelResponse,
        ) -> None:
            del context, agent, response
            observed.append("model_end")

        async def on_tool_start(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Tool,
        ) -> None:
            del context, agent, tool
            observed.append("tool_start")

        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Tool,
            result: object,
        ) -> None:
            del context, agent, tool, result
            observed.append("tool_end")

    @tool
    def inspect_release(value: str) -> str:
        """Inspect a release checkpoint for lifecycle hook ordering."""
        observed.append(f"tool_call:{value}")
        return "HOOK_CHECKPOINT_READY"

    agent = Agent(
        name="Packaged run hooks agent",
        model=integration_model,
        instructions=("Call inspect_release with value='release', then reply exactly HOOKS_READY."),
        tools=[inspect_release],
        model_settings={"max_tokens": 256},
    )
    hooks = RecordingHooks()
    config = RunConfig(tracing_disabled=True)
    result: RunResult | RunResultStreaming
    if streaming:
        result = Runner.run_streamed(agent, "Inspect the release.", hooks=hooks, run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "Inspect the release.", hooks=hooks, run_config=config)

    assert result.final_output == "HOOKS_READY"
    assert observed == [
        "agent_start",
        "model_start",
        "model_end",
        "tool_start",
        "tool_call:release",
        "tool_end",
        "model_start",
        "model_end",
        "agent_end",
    ]
