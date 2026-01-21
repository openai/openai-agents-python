from __future__ import annotations

import json

import pytest
from pydantic import BaseModel
from typing_extensions import TypedDict

from agents import (
    Agent,
    ItemHelpers,
    MaxTurnsExceeded,
    MessageOutputItem,
    RunErrorHandlerResult,
    Runner,
    UserError,
)
from agents.stream_events import RunItemStreamEvent

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


@pytest.mark.asyncio
async def test_non_streamed_max_turns():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )

    func_output = json.dumps({"a": "b"})

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_function_tool_call("some_function", func_output)],
            [get_text_message("2"), get_function_tool_call("some_function", func_output)],
            [get_text_message("3"), get_function_tool_call("some_function", func_output)],
            [get_text_message("4"), get_function_tool_call("some_function", func_output)],
            [get_text_message("5"), get_function_tool_call("some_function", func_output)],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="user_message", max_turns=3)


@pytest.mark.asyncio
async def test_streamed_max_turns():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )
    func_output = json.dumps({"a": "b"})

    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("1"),
                get_function_tool_call("some_function", func_output),
            ],
            [
                get_text_message("2"),
                get_function_tool_call("some_function", func_output),
            ],
            [
                get_text_message("3"),
                get_function_tool_call("some_function", func_output),
            ],
            [
                get_text_message("4"),
                get_function_tool_call("some_function", func_output),
            ],
            [
                get_text_message("5"),
                get_function_tool_call("some_function", func_output),
            ],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        output = Runner.run_streamed(agent, input="user_message", max_turns=3)
        async for _ in output.stream_events():
            pass


class Foo(TypedDict):
    a: str


class FooModel(BaseModel):
    summary: str


@pytest.mark.asyncio
async def test_structured_output_non_streamed_max_turns():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=Foo,
        tools=[get_function_tool("tool_1", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="user_message", max_turns=3)


@pytest.mark.asyncio
async def test_structured_output_streamed_max_turns():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=Foo,
        tools=[get_function_tool("tool_1", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        output = Runner.run_streamed(agent, input="user_message", max_turns=3)
        async for _ in output.stream_events():
            pass


@pytest.mark.asyncio
async def test_structured_output_max_turns_handler_invalid_output():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=Foo,
    )

    with pytest.raises(UserError):
        await Runner.run(
            agent,
            input="user_message",
            max_turns=0,
            error_handlers={"max_turns": lambda data: {"summary": "nope"}},
        )


@pytest.mark.asyncio
async def test_structured_output_max_turns_handler_pydantic_output():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=FooModel,
    )

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: FooModel(summary="ok")},
    )

    assert isinstance(result.final_output, FooModel)
    assert result.final_output.summary == "ok"
    assert ItemHelpers.text_message_outputs(result.new_items) == '{"summary":"ok"}'


@pytest.mark.asyncio
async def test_structured_output_max_turns_handler_list_output():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=list[str],
    )

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: ["a", "b"]},
    )

    assert result.final_output == ["a", "b"]
    assert ItemHelpers.text_message_outputs(result.new_items) == '{"response":["a","b"]}'


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_returns_output():
    model = FakeModel()
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(
                final_output=f"summary:{len(data.run_data.history)}"
            ),
        },
    )

    assert result.final_output == "summary:1"
    assert ItemHelpers.text_message_outputs(result.new_items) == "summary:1"


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_skip_history():
    model = FakeModel()
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(
                final_output="summary",
                include_in_history=False,
            ),
        },
    )

    assert result.final_output == "summary"
    assert result.new_items == []


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_raw_output():
    model = FakeModel()
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: "summary"},
    )

    assert result.final_output == "summary"
    assert ItemHelpers.text_message_outputs(result.new_items) == "summary"


@pytest.mark.asyncio
async def test_non_streamed_max_turns_handler_raw_dict_output():
    model = FakeModel()
    agent = Agent(name="test_1", model=model)

    result = await Runner.run(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: {"summary": "ok"}},
    )

    assert result.final_output == {"summary": "ok"}


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_returns_output():
    model = FakeModel()
    agent = Agent(name="test_1", model=model)

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={
            "max_turns": lambda data: RunErrorHandlerResult(final_output="summary"),
        },
    )

    events = [event async for event in result.stream_events()]
    assert result.final_output == "summary"
    run_item_events = [event for event in events if isinstance(event, RunItemStreamEvent)]
    assert len(run_item_events) == 1
    assert run_item_events[0].name == "message_output_created"
    assert isinstance(run_item_events[0].item, MessageOutputItem)
    assert ItemHelpers.text_message_output(run_item_events[0].item) == "summary"


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_pydantic_output():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=FooModel,
    )

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: FooModel(summary="ok")},
    )

    events = [event async for event in result.stream_events()]
    run_item_events = [event for event in events if isinstance(event, RunItemStreamEvent)]

    assert isinstance(result.final_output, FooModel)
    assert result.final_output.summary == "ok"
    assert len(run_item_events) == 1
    assert run_item_events[0].name == "message_output_created"
    assert isinstance(run_item_events[0].item, MessageOutputItem)
    assert ItemHelpers.text_message_output(run_item_events[0].item) == '{"summary":"ok"}'


@pytest.mark.asyncio
async def test_streamed_max_turns_handler_list_output():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=list[str],
    )

    result = Runner.run_streamed(
        agent,
        input="user_message",
        max_turns=0,
        error_handlers={"max_turns": lambda data: ["a", "b"]},
    )

    events = [event async for event in result.stream_events()]
    run_item_events = [event for event in events if isinstance(event, RunItemStreamEvent)]

    assert result.final_output == ["a", "b"]
    assert len(run_item_events) == 1
    assert run_item_events[0].name == "message_output_created"
    assert isinstance(run_item_events[0].item, MessageOutputItem)
    assert ItemHelpers.text_message_output(run_item_events[0].item) == '{"response":["a","b"]}'
