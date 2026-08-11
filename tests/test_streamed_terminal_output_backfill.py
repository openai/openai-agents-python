from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseInProgressEvent,
    ResponseOutputItemDoneEvent,
)

from agents import Agent, Runner
from agents.items import TResponseOutputItem, TResponseStreamEvent
from agents.testing import ModelStep, ScriptedModel
from agents.tool import function_tool

from .model_test_helpers import get_response_obj
from .test_responses import get_final_output_message, get_function_tool_call


def _stream_step(
    streamed_output: list[TResponseOutputItem],
    terminal_output: list[TResponseOutputItem],
) -> ModelStep:
    async def events(_call) -> AsyncIterator[TResponseStreamEvent]:
        terminal_response = get_response_obj(terminal_output)
        sequence_number = 0

        yield ResponseCreatedEvent(
            type="response.created", response=terminal_response, sequence_number=sequence_number
        )
        sequence_number += 1

        yield ResponseInProgressEvent(
            type="response.in_progress",
            response=terminal_response,
            sequence_number=sequence_number,
        )
        sequence_number += 1

        for output_index, output_item in enumerate(streamed_output):
            yield ResponseOutputItemDoneEvent(
                type="response.output_item.done",
                item=output_item,
                output_index=output_index,
                sequence_number=sequence_number,
            )
            sequence_number += 1

        yield ResponseCompletedEvent(
            type="response.completed",
            response=terminal_response,
            sequence_number=sequence_number,
        )

    return ModelStep.stream(events)


@pytest.mark.asyncio
async def test_streamed_runner_backfills_empty_terminal_output_before_step_resolution() -> None:
    tool_inputs: list[str] = []

    async def test_tool(a: str) -> str:
        tool_inputs.append(a)
        return "tool_result"

    tool = function_tool(test_tool, name_override="foo")
    model = ScriptedModel(
        [
            _stream_step(
                [get_function_tool_call("foo", json.dumps({"a": "b"}), call_id="call-1")],
                [],
            ),
            _stream_step([get_final_output_message("done")], [get_final_output_message("done")]),
        ]
    )
    agent = Agent(name="test", model=model, tools=[tool])

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert tool_inputs == ["b"]
    assert [item.type for item in result.raw_responses[0].output] == ["function_call"]
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_streamed_runner_preserves_populated_terminal_output() -> None:
    tool_inputs: list[str] = []

    async def test_tool(a: str) -> str:
        tool_inputs.append(a)
        return "tool_result"

    tool = function_tool(test_tool, name_override="foo")
    model = ScriptedModel(
        [
            _stream_step(
                [get_function_tool_call("foo", json.dumps({"a": "b"}), call_id="call-1")],
                [get_final_output_message("done")],
            )
        ]
    )
    agent = Agent(name="test", model=model, tools=[tool])

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert tool_inputs == []
    assert [item.type for item in result.raw_responses[0].output] == ["message"]
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_streamed_runner_backfills_multiple_tool_calls_in_order() -> None:
    tool_inputs: list[tuple[str, str]] = []

    async def foo_tool(a: str) -> str:
        tool_inputs.append(("foo", a))
        return "foo_result"

    async def bar_tool(b: str) -> str:
        tool_inputs.append(("bar", b))
        return "bar_result"

    foo = function_tool(foo_tool, name_override="foo")
    bar = function_tool(bar_tool, name_override="bar")
    model = ScriptedModel(
        [
            _stream_step(
                [
                    get_function_tool_call("foo", json.dumps({"a": "first"}), call_id="call-1"),
                    get_function_tool_call("bar", json.dumps({"b": "second"}), call_id="call-2"),
                ],
                [],
            ),
            _stream_step([get_final_output_message("done")], [get_final_output_message("done")]),
        ]
    )
    agent = Agent(name="test", model=model, tools=[foo, bar])

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert tool_inputs == [("foo", "first"), ("bar", "second")]
    assert [item.type for item in result.raw_responses[0].output] == [
        "function_call",
        "function_call",
    ]
    assert result.final_output == "done"
