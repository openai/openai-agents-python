import asyncio
import time

import pytest

from agents import Agent, HandoffCallItem, Runner, function_tool
from agents.extensions.handoff_filters import remove_all_tools
from agents.handoffs import handoff

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message


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
    tool_call_start_time = -1
    tool_call_end_time = -1
    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                tool_call_start_time = time.time_ns()
            elif event.item.type == "tool_call_output_item":
                tool_call_end_time = time.time_ns()

    assert tool_call_start_time > 0, "tool_call_item was not observed"
    assert tool_call_end_time > 0, "tool_call_output_item was not observed"
    assert tool_call_start_time < tool_call_end_time, "Tool call ended before or equals it started?"


@pytest.mark.asyncio
async def test_stream_events_main_with_handoff():
    @function_tool
    async def foo(args: str) -> str:
        return f"foo_result_{args}"

    english_agent = Agent(
        name="EnglishAgent",
        instructions="You only speak English.",
        model=FakeModel(),
    )

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("Hello"),
                get_function_tool_call("foo", '{"args": "arg1"}'),
                get_handoff_tool_call(english_agent),
            ],
            [get_text_message("Done")],
        ]
    )

    triage_agent = Agent(
        name="TriageAgent",
        instructions="Handoff to the appropriate agent based on the language of the request.",
        handoffs=[
            handoff(english_agent, input_filter=remove_all_tools),
        ],
        tools=[foo],
        model=model,
    )

    result = Runner.run_streamed(
        triage_agent,
        input="Start",
    )

    handoff_requested_seen = False
    agent_switched_to_english = False

    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            if isinstance(event.item, HandoffCallItem):
                handoff_requested_seen = True
        elif event.type == "agent_updated_stream_event":
            if hasattr(event, "new_agent") and event.new_agent.name == "EnglishAgent":
                agent_switched_to_english = True

    assert handoff_requested_seen, "handoff_requested event not observed"
    assert agent_switched_to_english, "Agent did not switch to EnglishAgent"
