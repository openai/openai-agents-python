"""Caller-visible recovery for failed Runner handoff invocations."""

import asyncio
from typing import Any, Literal
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    Handoff,
    ModelBehaviorError,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    SQLiteSession,
    default_tool_error_function,
    handoff,
)
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call


class Pick(BaseModel):
    name: Literal["a"]


@pytest.mark.parametrize("streamed", [False, True])
async def test_failed_handoff_retries_on_caller_then_transfers(streamed: bool) -> None:
    received: list[str] = []

    async def on_handoff(ctx: RunContextWrapper[Any], data: Pick) -> None:
        received.append(data.name)

    @tool
    def companion() -> str:
        return "companion result"

    target_model = ScriptedModel([[assistant_message("target answer")]])
    target = Agent(name="target", model=target_model)
    input_filter = Mock(side_effect=lambda data: data)
    transfer = handoff(
        target,
        on_handoff=on_handoff,
        input_type=Pick,
        input_filter=input_filter,
        failure_error_function=default_tool_error_function,
    )
    caller_model = ScriptedModel(
        [
            [
                function_call("companion", {}, call_id="companion"),
                function_call(transfer.tool_name, {"name": "b"}, call_id="bad"),
            ],
            [function_call(transfer.tool_name, {"name": "a"}, call_id="good")],
        ]
    )
    caller = Agent(name="caller", model=caller_model, tools=[companion], handoffs=[transfer])
    hooks = RunHooks[Any]()
    hooks.on_handoff = AsyncMock()
    session = SQLiteSession("handoff-retry")
    events = []
    try:
        if streamed:
            result = Runner.run_streamed(caller, "go", hooks=hooks, session=session)
            async for event in result.stream_events():
                events.append(event)
        else:
            result = await Runner.run(caller, "go", hooks=hooks, session=session)
        history = await session.get_items()
    finally:
        session.close()

    assert result.final_output == "target answer"
    assert result.last_agent is target
    assert received == ["a"]
    assert len(caller_model.calls) == 2
    assert len(target_model.calls) == 1
    input_filter.assert_called_once()
    hooks.on_handoff.assert_awaited_once()
    assert hooks.on_handoff.call_args.kwargs["from_agent"] is caller
    assert hooks.on_handoff.call_args.kwargs["to_agent"] is target
    expected = {
        "companion": "companion result",
        "bad": "An error occurred while running the tool. Please try again.",
    }
    for items in (caller_model.calls[1].input, target_model.calls[0].input, history):
        for call_id, output in expected.items():
            assert [
                item["output"]
                for item in items
                if item.get("type") == "function_call_output" and item["call_id"] == call_id
            ] == [output]
    assert len([item for item in result.new_items if item.type == "handoff_output_item"]) == 1
    if streamed:
        assert [
            event.new_agent.name for event in events if event.type == "agent_updated_stream_event"
        ] == ["caller", "target"]
        assert (
            len(
                [
                    event
                    for event in events
                    if event.type == "run_item_stream_event" and event.name == "handoff_occured"
                ]
            )
            == 1
        )


@pytest.mark.parametrize("streamed", [False, True])
async def test_default_handoff_failure_still_raises(streamed: bool) -> None:
    callback = Mock()
    target_model = ScriptedModel()
    target = Agent(name="target", model=target_model)

    def on_handoff(ctx: RunContextWrapper[Any], data: Pick) -> None:
        callback(data)

    transfer = handoff(target, input_type=Pick, on_handoff=on_handoff)
    model = ScriptedModel([[function_call(transfer.tool_name, {"name": "b"}, call_id="bad")]])
    caller = Agent(name="caller", model=model, handoffs=[transfer])
    with pytest.raises(ModelBehaviorError):
        if streamed:
            result = Runner.run_streamed(caller, "go")
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(caller, "go")
    callback.assert_not_called()
    assert len(model.calls) == 1
    assert not target_model.calls


@pytest.mark.parametrize("async_formatter", [False, True])
async def test_custom_handoff_can_handle_or_reraise_application_error(
    async_formatter: bool,
) -> None:
    error = ValueError("synthetic application failure")
    seen: list[Exception] = []

    async def invoke(ctx: RunContextWrapper[Any], arguments: str) -> Agent[Any]:
        raise error

    def format_error(ctx: RunContextWrapper[Any], caught: Exception) -> str:
        seen.append(caught)
        return "Transfer unavailable; answer here."

    async def async_format_error(ctx: RunContextWrapper[Any], caught: Exception) -> str:
        return format_error(ctx, caught)

    transfer = Handoff(
        "transfer",
        "Transfer to target",
        {},
        invoke,
        "target",
        failure_error_function=async_format_error if async_formatter else format_error,
    )
    model = ScriptedModel(
        [[function_call("transfer", {}, call_id="call")], [assistant_message("caller answer")]]
    )
    caller = Agent(name="caller", model=model, handoffs=[transfer])
    result = await Runner.run(caller, "go")
    assert result.final_output == "caller answer"
    assert result.last_agent is caller
    assert seen == [error]
    assert model.calls[1].input[-1] == {
        "type": "function_call_output",
        "call_id": "call",
        "output": "Transfer unavailable; answer here.",
    }
    assert not any(item.type == "handoff_output_item" for item in result.new_items)

    def reraise(ctx: RunContextWrapper[Any], caught: Exception) -> str:
        raise caught

    transfer.failure_error_function = reraise
    caller.model = ScriptedModel([[function_call("transfer", {}, call_id="call")]])
    with pytest.raises(ValueError) as raised:
        await Runner.run(caller, "go")
    assert raised.value is error


@pytest.mark.parametrize("failure", ["cancel", "formatter", "filter"])
async def test_failures_outside_recovery_boundary_propagate(failure: str) -> None:
    formatter = Mock(return_value="handled")
    target = Agent(name="target", model=ScriptedModel())

    async def on_handoff(ctx: RunContextWrapper[Any]) -> None:
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "formatter":
            raise ValueError("invocation failed")

    def input_filter(data):
        raise RuntimeError("filter failed")

    if failure == "formatter":
        formatter.side_effect = RuntimeError("formatter failed")
    transfer = handoff(
        target,
        on_handoff=on_handoff,
        input_filter=input_filter if failure == "filter" else None,
        failure_error_function=formatter,
    )
    caller = Agent(
        name="caller",
        model=ScriptedModel([[function_call(transfer.tool_name, {}, call_id="call")]]),
        handoffs=[transfer],
    )
    expected_error = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected_error):
        await Runner.run(caller, "go", run_config=RunConfig(trace_include_sensitive_data=False))
    assert formatter.call_count == (1 if failure == "formatter" else 0)


def test_handoff_recovery_run_sync() -> None:
    async def on_handoff(ctx: RunContextWrapper[Any], data: Pick) -> None:
        pytest.fail("invalid input must not invoke callback")

    transfer = handoff(
        Agent(name="target"),
        input_type=Pick,
        on_handoff=on_handoff,
        failure_error_function=default_tool_error_function,
    )
    model = ScriptedModel(
        [
            [function_call(transfer.tool_name, {"name": "b"}, call_id="bad")],
            [assistant_message("done")],
        ]
    )
    caller = Agent(name="caller", model=model, handoffs=[transfer])
    result = Runner.run_sync(caller, "go")
    assert result.final_output == "done"
    assert result.last_agent is caller


async def test_factory_callback_recovery_does_not_expose_error_or_undo_side_effects() -> None:
    from .testing_processor import fetch_span_errors

    effects: list[str] = []

    async def on_handoff(ctx: RunContextWrapper[Any]) -> None:
        effects.append("callback ran")
        raise ValueError("synthetic-private-detail")

    transfer = handoff(
        Agent(name="target"),
        on_handoff=on_handoff,
        failure_error_function=default_tool_error_function,
    )
    model = ScriptedModel(
        [[function_call(transfer.tool_name, {}, call_id="call")], [assistant_message("done")]]
    )
    caller = Agent(name="caller", model=model, handoffs=[transfer])
    result = await Runner.run(
        caller, "go", run_config=RunConfig(trace_include_sensitive_data=False)
    )
    assert result.final_output == "done"
    assert effects == ["callback ran"]
    assert "synthetic-private-detail" not in str(model.calls[1].input)
    assert fetch_span_errors("handoff") == [{"message": "Handoff invocation failed", "data": {}}]
