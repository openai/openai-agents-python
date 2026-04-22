from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseInProgressEvent,
    ResponseOutputItemDoneEvent,
)

from agents import Agent, Runner
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import TResponseInputItem, TResponseOutputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.tool import Tool, function_tool

from .fake_model import FakeModel, get_response_obj
from .test_responses import get_final_output_message, get_function_tool_call


class TerminalOutputStreamModel(FakeModel):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_turn_outputs: list[list[TResponseOutputItem]] = []

    def add_terminal_turn_outputs(
        self,
        outputs: list[list[TResponseOutputItem]],
    ) -> None:
        self.terminal_turn_outputs.extend(outputs)

    def get_next_terminal_output(self) -> list[TResponseOutputItem]:
        if not self.terminal_turn_outputs:
            return []
        return self.terminal_turn_outputs.pop(0)

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        turn_args = {
            "system_instructions": system_instructions,
            "input": input,
            "model_settings": model_settings,
            "tools": tools,
            "output_schema": output_schema,
            "previous_response_id": previous_response_id,
            "conversation_id": conversation_id,
        }

        if self.first_turn_args is None:
            self.first_turn_args = turn_args.copy()

        self.last_turn_args = turn_args
        streamed_output = self.get_next_output()
        if isinstance(streamed_output, Exception):
            raise streamed_output

        terminal_response = get_response_obj(
            self.get_next_terminal_output(),
            usage=self.hardcoded_usage,
        )
        sequence_number = 0

        yield ResponseCreatedEvent(
            type="response.created",
            response=terminal_response,
            sequence_number=sequence_number,
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


@pytest.mark.asyncio
async def test_streamed_runner_backfills_empty_terminal_output_before_step_resolution() -> None:
    tool_inputs: list[str] = []

    async def test_tool(a: str) -> str:
        tool_inputs.append(a)
        return "tool_result"

    tool = function_tool(test_tool, name_override="foo")
    model = TerminalOutputStreamModel()
    agent = Agent(name="test", model=model, tools=[tool])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("foo", json.dumps({"a": "b"}), call_id="call-1")],
            [get_final_output_message("done")],
        ]
    )
    model.add_terminal_turn_outputs(
        [
            [],
            [get_final_output_message("done")],
        ]
    )

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
    model = TerminalOutputStreamModel()
    agent = Agent(name="test", model=model, tools=[tool])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("foo", json.dumps({"a": "b"}), call_id="call-1")],
        ]
    )
    model.add_terminal_turn_outputs(
        [
            [get_final_output_message("done")],
        ]
    )

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
    model = TerminalOutputStreamModel()
    agent = Agent(name="test", model=model, tools=[foo, bar])

    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("foo", json.dumps({"a": "first"}), call_id="call-1"),
                get_function_tool_call("bar", json.dumps({"b": "second"}), call_id="call-2"),
            ],
            [get_final_output_message("done")],
        ]
    )
    model.add_terminal_turn_outputs(
        [
            [],
            [get_final_output_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert tool_inputs == [("foo", "first"), ("bar", "second")]
    assert [item.type for item in result.raw_responses[0].output] == [
        "function_call",
        "function_call",
    ]
    assert result.final_output == "done"
