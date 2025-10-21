"""Tests for streaming tool output functionality."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from agents import Agent, Runner, ToolOutputStreamEvent, function_tool
from agents.items import ToolCallOutputItem

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


@function_tool
async def streaming_counter() -> AsyncIterator[str]:
    """A simple streaming tool that counts from 1 to 5."""
    for i in range(1, 6):
        await asyncio.sleep(0.01)  # Small delay to simulate processing
        yield f"{i}... "


@function_tool
async def streaming_search(query: str) -> AsyncIterator[str]:
    """A streaming search tool that returns results incrementally."""
    results = [
        f"Searching for '{query}'...\n",
        "Found result 1\n",
        "Found result 2\n",
        "Found result 3\n",
        "Search complete!\n",
    ]
    for result in results:
        await asyncio.sleep(0.01)
        yield result


@function_tool
async def non_streaming_tool() -> str:
    """A traditional non-streaming tool for comparison."""
    await asyncio.sleep(0.01)
    return "Non-streaming result"


@pytest.mark.asyncio
async def test_basic_streaming_tool():
    """Test that a streaming tool emits ToolOutputStreamEvent events."""
    model = FakeModel()
    agent = Agent(
        name="StreamingAgent",
        model=model,
        tools=[streaming_counter],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: call the streaming tool
            [get_function_tool_call("streaming_counter", "{}")],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="Count to 5")

    # Collect all events
    events = []
    async for event in result.stream_events():
        events.append(event)

    # Verify we received ToolOutputStreamEvent events
    tool_stream_events = [e for e in events if e.type == "tool_output_stream_event"]
    assert len(tool_stream_events) > 0, "Should have received streaming events"

    # Verify all streaming events are ToolOutputStreamEvent instances
    for event in tool_stream_events:
        assert isinstance(event, ToolOutputStreamEvent)
        assert event.tool_name == "streaming_counter"
        assert event.tool_call_id is not None
        assert event.delta is not None

    # Verify we also received the final tool_output event
    tool_output_events = [
        e for e in events if e.type == "run_item_stream_event" and e.name == "tool_output"
    ]
    assert len(tool_output_events) == 1, "Should have received final tool output event"

    # Verify the final output contains all chunks combined
    assert result.final_output is not None


@pytest.mark.asyncio
async def test_streaming_tool_with_arguments():
    """Test a streaming tool that accepts arguments."""
    model = FakeModel()
    agent = Agent(
        name="SearchAgent",
        model=model,
        tools=[streaming_search],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: call the streaming search tool
            [get_function_tool_call("streaming_search", '{"query": "AI"}')],
            # Second turn: text message
            [get_text_message("Search completed")],
        ]
    )

    result = Runner.run_streamed(agent, input="Search for AI")

    # Collect streaming events
    stream_deltas = []
    async for event in result.stream_events():
        if event.type == "tool_output_stream_event":
            assert isinstance(event, ToolOutputStreamEvent)
            stream_deltas.append(event.delta)

    # Verify we received multiple chunks
    assert len(stream_deltas) > 0, "Should have received streaming output"

    # Verify the accumulated output makes sense
    full_output = "".join(stream_deltas)
    assert "Searching for 'AI'" in full_output
    assert "Found result" in full_output
    assert "Search complete!" in full_output


@pytest.mark.asyncio
async def test_mixed_streaming_and_non_streaming_tools():
    """Test that both streaming and non-streaming tools work together."""
    model = FakeModel()
    agent = Agent(
        name="MixedAgent",
        model=model,
        tools=[streaming_counter, non_streaming_tool],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: call both tools
            [
                get_function_tool_call("streaming_counter", "{}"),
                get_function_tool_call("non_streaming_tool", "{}"),
            ],
            # Second turn: text message
            [get_text_message("Both tools completed")],
        ]
    )

    result = Runner.run_streamed(agent, input="Run both tools")

    # Collect events
    streaming_events = []
    tool_output_events = []

    async for event in result.stream_events():
        if event.type == "tool_output_stream_event":
            streaming_events.append(event)
        elif event.type == "run_item_stream_event" and event.name == "tool_output":
            tool_output_events.append(event)

    # Verify only the streaming tool emitted stream events
    assert len(streaming_events) > 0, "Should have streaming events from streaming_counter"
    assert all(e.tool_name == "streaming_counter" for e in streaming_events), (
        "Streaming events should only be from streaming tool"
    )

    # Verify both tools produced final outputs
    assert len(tool_output_events) == 2, "Should have 2 tool output events"


@pytest.mark.asyncio
async def test_streaming_tool_accumulation():
    """Test that streaming tool output is properly accumulated."""
    model = FakeModel()
    agent = Agent(
        name="AccumulationAgent",
        model=model,
        tools=[streaming_counter],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("streaming_counter", "{}")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="Count")

    # Collect all deltas
    accumulated = []
    final_output = None

    async for event in result.stream_events():
        if event.type == "tool_output_stream_event":
            assert isinstance(event, ToolOutputStreamEvent)
            accumulated.append(event.delta)
        elif event.type == "run_item_stream_event" and event.name == "tool_output":
            assert isinstance(event.item, ToolCallOutputItem)
            final_output = str(event.item.output)

    # Verify accumulated output matches final output
    accumulated_str = "".join(accumulated)
    assert accumulated_str == final_output, "Accumulated output should match final output"
    assert accumulated_str == "1... 2... 3... 4... 5... ", "Output should be correct"


@pytest.mark.asyncio
async def test_streaming_tool_in_non_streaming_mode():
    """Test that streaming tools work correctly in non-streaming mode."""
    model = FakeModel()
    agent = Agent(
        name="NonStreamingAgent",
        model=model,
        tools=[streaming_counter],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("streaming_counter", "{}")],
            [get_text_message("done")],
        ]
    )

    # Use regular run instead of run_streamed
    result = await Runner.run(agent, input="Count")

    # The result should still work, just without streaming events
    assert result.final_output is not None
    # The tool should have been executed successfully
    tool_outputs = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert len(tool_outputs) == 1
    assert str(tool_outputs[0].output) == "1... 2... 3... 4... 5... "


@pytest.mark.asyncio
async def test_streaming_tool_agent_association():
    """Test that streaming events contain correct agent information."""
    model = FakeModel()
    agent = Agent(
        name="TestAgent",
        model=model,
        tools=[streaming_counter],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("streaming_counter", "{}")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="Count")

    async for event in result.stream_events():
        if event.type == "tool_output_stream_event":
            assert isinstance(event, ToolOutputStreamEvent)
            assert event.agent.name == "TestAgent"
            assert event.agent == agent
