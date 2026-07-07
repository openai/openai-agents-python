from __future__ import annotations

import json
from typing import Any

import pytest
from openai.types.responses import ResponseOutputMessage
from pydantic import BaseModel

from agents import (
    Agent,
    AgentHookContext,
    GuardrailFunctionOutput,
    ItemHelpers,
    MessageOutputItem,
    ModelBehaviorError,
    OutputGuardrail,
    RunContextWrapper,
    RunErrorHandlerInput,
    RunErrorHandlerResult,
    RunErrorHandlers,
    RunHooks,
    Runner,
    UserError,
    function_tool,
)
from agents.items import TResponseInputItem, TResponseOutputItem
from agents.stream_events import RunItemStreamEvent

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message
from .utils.simple_session import SimpleListSession


class FinalOutput(BaseModel):
    summary: str


class RecordingRunHooks(RunHooks[None]):
    def __init__(self) -> None:
        self.final_outputs: list[Any] = []

    async def on_agent_end(
        self,
        context: AgentHookContext[None],
        agent: Agent[None],
        output: Any,
    ) -> None:
        self.final_outputs.append(output)


def _message_texts(items: list[TResponseInputItem]) -> list[str]:
    texts: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        message = ResponseOutputMessage.model_validate(item)
        texts.append(ItemHelpers.extract_text(message) or "")
    return texts


@pytest.mark.asyncio
async def test_invalid_final_output_raises_without_handler() -> None:
    model = FakeModel(initial_output=[get_text_message("not valid json")])
    agent = Agent(name="test", model=model, output_type=FinalOutput)

    with pytest.raises(ModelBehaviorError, match="Invalid JSON"):
        await Runner.run(agent, input="user_message")


@pytest.mark.asyncio
async def test_invalid_final_output_handler_returns_validated_fallback() -> None:
    model = FakeModel(initial_output=[get_text_message("not valid json")])
    agent = Agent(name="test", model=model, output_type=FinalOutput)

    def handler(data: RunErrorHandlerInput[None]) -> FinalOutput:
        assert isinstance(data.error, ModelBehaviorError)
        assert data.run_data.raw_responses
        assert ItemHelpers.text_message_outputs(data.run_data.new_items) == "not valid json"
        return FinalOutput(summary="safe fallback")

    result = await Runner.run(
        agent,
        input="user_message",
        error_handlers={"invalid_final_output": handler},
    )

    assert result.final_output == FinalOutput(summary="safe fallback")
    assert [
        ItemHelpers.text_message_output(item)
        for item in result.new_items
        if isinstance(item, MessageOutputItem)
    ] == ["not valid json", '{"summary":"safe fallback"}']


@pytest.mark.asyncio
async def test_invalid_final_output_handler_can_skip_fallback_history() -> None:
    model = FakeModel(initial_output=[get_text_message("not valid json")])
    agent = Agent(name="test", model=model, output_type=FinalOutput)

    result = await Runner.run(
        agent,
        input="user_message",
        error_handlers={
            "invalid_final_output": lambda _data: RunErrorHandlerResult(
                final_output=FinalOutput(summary="safe fallback"),
                include_in_history=False,
            )
        },
    )

    assert result.final_output == FinalOutput(summary="safe fallback")
    assert ItemHelpers.text_message_outputs(result.new_items) == "not valid json"


@pytest.mark.asyncio
async def test_invalid_final_output_handler_rejects_invalid_fallback() -> None:
    model = FakeModel(initial_output=[get_text_message("not valid json")])
    agent = Agent(name="test", model=model, output_type=FinalOutput)

    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        with pytest.raises(UserError, match="Invalid run error handler final_output"):
            await Runner.run(
                agent,
                input="user_message",
                error_handlers={"invalid_final_output": lambda _data: {"unexpected": "value"}},
            )


@pytest.mark.asyncio
async def test_invalid_final_output_handler_can_decline_recovery() -> None:
    model = FakeModel(initial_output=[get_text_message("not valid json")])
    agent = Agent(name="test", model=model, output_type=FinalOutput)

    with pytest.raises(ModelBehaviorError, match="Invalid JSON"):
        await Runner.run(
            agent,
            input="user_message",
            error_handlers={"invalid_final_output": lambda _data: None},
        )


@pytest.mark.asyncio
async def test_invalid_final_output_handler_does_not_catch_other_model_behavior_errors() -> None:
    model = FakeModel(initial_output=[get_function_tool_call("missing_tool")])
    agent = Agent(name="test", model=model, output_type=FinalOutput)
    handler_called = False

    def handler(_data: RunErrorHandlerInput[None]) -> FinalOutput:
        nonlocal handler_called
        handler_called = True
        return FinalOutput(summary="safe fallback")

    with pytest.raises(ModelBehaviorError, match="not found"):
        await Runner.run(
            agent,
            input="user_message",
            error_handlers={"invalid_final_output": handler},
        )

    assert not handler_called


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_output", [[], [get_text_message("")]])
async def test_empty_structured_output_handler_avoids_another_model_turn(
    invalid_output: list[TResponseOutputItem],
) -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs([invalid_output, [get_text_message('{"summary":"unused"}')]])
    agent = Agent(name="test", model=model, output_type=FinalOutput)

    def handler(data: RunErrorHandlerInput[None]) -> FinalOutput:
        assert isinstance(data.error, ModelBehaviorError)
        assert data.error.message == (
            "Model returned no final output for the structured output type."
        )
        return FinalOutput(summary="safe fallback")

    result = await Runner.run(
        agent,
        input="user_message",
        error_handlers={"invalid_final_output": handler},
    )

    assert result.final_output == FinalOutput(summary="safe fallback")
    assert len(model.turn_outputs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_handlers",
    [None, {"invalid_final_output": lambda _data: None}],
)
async def test_empty_structured_output_without_fallback_keeps_existing_next_turn_behavior(
    error_handlers: RunErrorHandlers[None] | None,
) -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs([[], [get_text_message('{"summary":"second turn"}')]])
    agent = Agent(name="test", model=model, output_type=FinalOutput)

    result = await Runner.run(agent, input="user_message", error_handlers=error_handlers)

    assert result.final_output == FinalOutput(summary="second turn")
    assert not model.turn_outputs


@pytest.mark.asyncio
async def test_streamed_invalid_final_output_emits_exact_fallback_item() -> None:
    model = FakeModel(initial_output=[get_text_message("not valid json")])
    agent = Agent(name="test", model=model, output_type=FinalOutput)
    session = SimpleListSession()

    result = Runner.run_streamed(
        agent,
        input="user_message",
        session=session,
        error_handlers={"invalid_final_output": lambda _data: FinalOutput(summary="safe fallback")},
    )
    events = [event async for event in result.stream_events()]

    assert result.final_output == FinalOutput(summary="safe fallback")
    fallback_events = [
        event
        for event in events
        if isinstance(event, RunItemStreamEvent)
        and event.name == "message_output_created"
        and isinstance(event.item, MessageOutputItem)
        and ItemHelpers.text_message_output(event.item) == '{"summary":"safe fallback"}'
    ]
    assert len(fallback_events) == 1
    assert [
        ItemHelpers.text_message_output(item)
        for item in result.new_items
        if isinstance(item, MessageOutputItem)
    ] == ["not valid json", '{"summary":"safe fallback"}']
    assert _message_texts(await session.get_items()) == [
        "not valid json",
        '{"summary":"safe fallback"}',
    ]


@pytest.mark.asyncio
async def test_streamed_empty_structured_output_handler_avoids_another_model_turn() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs([[], [get_text_message('{"summary":"unused"}')]])
    agent = Agent(name="test", model=model, output_type=FinalOutput)

    result = Runner.run_streamed(
        agent,
        input="user_message",
        error_handlers={"invalid_final_output": lambda _data: FinalOutput(summary="safe fallback")},
    )
    events = [event async for event in result.stream_events()]

    assert result.final_output == FinalOutput(summary="safe fallback")
    assert len(model.turn_outputs) == 1
    assert any(
        isinstance(event, RunItemStreamEvent)
        and event.name == "message_output_created"
        and isinstance(event.item, MessageOutputItem)
        and ItemHelpers.text_message_output(event.item) == '{"summary":"safe fallback"}'
        for event in events
    )


@pytest.mark.asyncio
async def test_invalid_final_output_fallback_runs_hooks_and_output_guardrails() -> None:
    model = FakeModel(initial_output=[get_text_message("not valid json")])
    hooks = RecordingRunHooks()
    guarded_outputs: list[Any] = []

    def record_output(
        context: RunContextWrapper[None],
        agent: Agent[Any],
        output: Any,
    ) -> GuardrailFunctionOutput:
        guarded_outputs.append(output)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    agent = Agent(
        name="test",
        model=model,
        output_type=FinalOutput,
        output_guardrails=[OutputGuardrail(guardrail_function=record_output)],
    )

    result = await Runner.run(
        agent,
        input="user_message",
        hooks=hooks,
        error_handlers={"invalid_final_output": lambda _data: FinalOutput(summary="safe fallback")},
    )

    expected = FinalOutput(summary="safe fallback")
    assert result.final_output == expected
    assert hooks.final_outputs == [expected]
    assert guarded_outputs == [expected]
    assert len(result.output_guardrail_results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_invalid_final_output_fallback_does_not_retry_or_replay_tools(
    streamed: bool,
) -> None:
    side_effects: list[str] = []

    @function_tool
    async def record_side_effect(value: str) -> str:
        side_effects.append(value)
        return f"recorded:{value}"

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "record_side_effect",
                    json.dumps({"value": "once"}),
                    call_id="first_call",
                )
            ],
            [get_text_message("not valid json")],
            [
                get_function_tool_call(
                    "record_side_effect",
                    json.dumps({"value": "replayed"}),
                    call_id="replayed_call",
                )
            ],
            [get_text_message('{"summary":"unexpected retry"}')],
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[record_side_effect],
        output_type=FinalOutput,
    )
    error_handlers: RunErrorHandlers[None] = {
        "invalid_final_output": lambda _data: FinalOutput(summary="safe fallback")
    }

    if streamed:
        streamed_result = Runner.run_streamed(
            agent,
            input="user_message",
            error_handlers=error_handlers,
        )
        events = [event async for event in streamed_result.stream_events()]
        final_output = streamed_result.final_output
        fallback_events = [
            event
            for event in events
            if isinstance(event, RunItemStreamEvent)
            and event.name == "message_output_created"
            and isinstance(event.item, MessageOutputItem)
            and ItemHelpers.text_message_output(event.item) == '{"summary":"safe fallback"}'
        ]
        assert len(fallback_events) == 1
    else:
        result = await Runner.run(
            agent,
            input="user_message",
            error_handlers=error_handlers,
        )
        final_output = result.final_output

    assert final_output == FinalOutput(summary="safe fallback")
    assert side_effects == ["once"]
    assert len(model.turn_outputs) == 2
