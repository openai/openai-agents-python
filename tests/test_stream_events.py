import asyncio
import time

import pytest

from agents import Agent, ItemHelpers, Runner, function_tool

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


@function_tool
async def foo() -> str:
    await asyncio.sleep(3)
    return "success!"

@pytest.mark.asyncio
async def test_stream_events_main():
    model = FakeModel()
    agent = Agent(
        name="Joker",
        model=model,
        tools=[foo],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [
                get_text_message("a_message"),
                get_function_tool_call("foo", ""),
            ],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(
        agent,
        input="Hello",
    )
    print("=== Run starting ===")
    tool_call_start_time = -1
    tool_call_end_time = -1
    async for event in result.stream_events():
        # We'll ignore the raw responses event deltas
        if event.type == "raw_response_event":
            continue
        elif event.type == "agent_updated_stream_event":
            print(f"Agent updated: {event.new_agent.name}")
        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                tool_call_start_time = time.time_ns()
                print(f"-- Tool was called at {tool_call_start_time}")
            elif event.item.type == "tool_call_output_item":
                tool_call_end_time = time.time_ns()
                print(f"-- Tool output: {event.item.output} at {tool_call_end_time}")
            elif event.item.type == "message_output_item":
                print(
                    f"-- Message output:\n {ItemHelpers.text_message_output(event.item)}"
                )

    print("=== Run complete ===")
    assert tool_call_start_time > 0, "tool_call_item was not observed"
    assert tool_call_end_time > 0, "tool_call_output_item was not observed"
    assert tool_call_start_time < tool_call_end_time, "Tool call ended before or equals it started?"
