import asyncio
import time

import pytest
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseInProgressEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningSummaryPartDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary

from agents import Agent, HandoffCallItem, Runner, function_tool
from agents.extensions.handoff_filters import remove_all_tools
from agents.handoffs import handoff
from agents.items import MessageOutputItem, ReasoningItem, ToolCallItem, ToolCallOutputItem

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message


def get_reasoning_item() -> ResponseReasoningItem:
    return ResponseReasoningItem(
        id="rid", type="reasoning", summary=[Summary(text="thinking", type="summary_text")]
    )


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


@pytest.mark.asyncio
async def test_complete_streaming_events():
    """Verify all streaming event types are emitted in correct order.

    Tests the complete event sequence including:
    - Reasoning items with summary events
    - Function call with arguments delta/done events
    - Message output with content_part and text delta/done events
    """
    model = FakeModel()
    agent = Agent(
        name="TestAgent",
        model=model,
        tools=[foo],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_reasoning_item(),
                get_function_tool_call("foo", '{"arg": "value"}'),
            ],
            [get_text_message("Final response")],
        ]
    )

    result = Runner.run_streamed(agent, input="Hello")

    events = []
    async for event in result.stream_events():
        events.append(event)

    assert len(events) == 27, f"Expected 27 events but got {len(events)}"

    # Event 0: agent_updated_stream_event
    assert events[0].type == "agent_updated_stream_event"
    assert events[0].new_agent.name == "TestAgent"

    # Event 1: ResponseCreatedEvent (first turn started)
    assert events[1].type == "raw_response_event"
    assert isinstance(events[1].data, ResponseCreatedEvent)

    # Event 2: ResponseInProgressEvent
    assert events[2].type == "raw_response_event"
    assert isinstance(events[2].data, ResponseInProgressEvent)

    # Event 3: ResponseOutputItemAddedEvent (reasoning item)
    assert events[3].type == "raw_response_event"
    assert isinstance(events[3].data, ResponseOutputItemAddedEvent)

    # Event 4: ResponseReasoningSummaryPartAddedEvent
    assert events[4].type == "raw_response_event"
    assert isinstance(events[4].data, ResponseReasoningSummaryPartAddedEvent)

    # Event 5: ResponseReasoningSummaryTextDeltaEvent
    assert events[5].type == "raw_response_event"
    assert isinstance(events[5].data, ResponseReasoningSummaryTextDeltaEvent)

    # Event 6: ResponseReasoningSummaryTextDoneEvent
    assert events[6].type == "raw_response_event"
    assert isinstance(events[6].data, ResponseReasoningSummaryTextDoneEvent)

    # Event 7: ResponseReasoningSummaryPartDoneEvent
    assert events[7].type == "raw_response_event"
    assert isinstance(events[7].data, ResponseReasoningSummaryPartDoneEvent)

    # Event 8: ResponseOutputItemDoneEvent (reasoning item)
    assert events[8].type == "raw_response_event"
    assert isinstance(events[8].data, ResponseOutputItemDoneEvent)

    # Event 9: ReasoningItem run_item_stream_event
    assert events[9].type == "run_item_stream_event"
    assert events[9].name == "reasoning_item_created"
    assert isinstance(events[9].item, ReasoningItem)

    # Event 10: ResponseOutputItemAddedEvent (function call)
    assert events[10].type == "raw_response_event"
    assert isinstance(events[10].data, ResponseOutputItemAddedEvent)

    # Event 11: ResponseFunctionCallArgumentsDeltaEvent
    assert events[11].type == "raw_response_event"
    assert isinstance(events[11].data, ResponseFunctionCallArgumentsDeltaEvent)

    # Event 12: ResponseFunctionCallArgumentsDoneEvent
    assert events[12].type == "raw_response_event"
    assert isinstance(events[12].data, ResponseFunctionCallArgumentsDoneEvent)

    # Event 13: ResponseOutputItemDoneEvent (function call)
    assert events[13].type == "raw_response_event"
    assert isinstance(events[13].data, ResponseOutputItemDoneEvent)

    # Event 14: ToolCallItem run_item_stream_event
    assert events[14].type == "run_item_stream_event"
    assert events[14].name == "tool_called"
    assert isinstance(events[14].item, ToolCallItem)

    # Event 15: ResponseCompletedEvent (first turn ended)
    assert events[15].type == "raw_response_event"
    assert isinstance(events[15].data, ResponseCompletedEvent)

    # Event 16: ToolCallOutputItem run_item_stream_event
    assert events[16].type == "run_item_stream_event"
    assert events[16].name == "tool_output"
    assert isinstance(events[16].item, ToolCallOutputItem)

    # Event 17: ResponseCreatedEvent (second turn started)
    assert events[17].type == "raw_response_event"
    assert isinstance(events[17].data, ResponseCreatedEvent)

    # Event 18: ResponseInProgressEvent
    assert events[18].type == "raw_response_event"
    assert isinstance(events[18].data, ResponseInProgressEvent)

    # Event 19: ResponseOutputItemAddedEvent
    assert events[19].type == "raw_response_event"
    assert isinstance(events[19].data, ResponseOutputItemAddedEvent)

    # Event 20: ResponseContentPartAddedEvent
    assert events[20].type == "raw_response_event"
    assert isinstance(events[20].data, ResponseContentPartAddedEvent)

    # Event 21: ResponseTextDeltaEvent
    assert events[21].type == "raw_response_event"
    assert isinstance(events[21].data, ResponseTextDeltaEvent)

    # Event 22: ResponseTextDoneEvent
    assert events[22].type == "raw_response_event"
    assert isinstance(events[22].data, ResponseTextDoneEvent)

    # Event 23: ResponseContentPartDoneEvent
    assert events[23].type == "raw_response_event"
    assert isinstance(events[23].data, ResponseContentPartDoneEvent)

    # Event 24: ResponseOutputItemDoneEvent
    assert events[24].type == "raw_response_event"
    assert isinstance(events[24].data, ResponseOutputItemDoneEvent)

    # Event 25: ResponseCompletedEvent (second turn ended)
    assert events[25].type == "raw_response_event"
    assert isinstance(events[25].data, ResponseCompletedEvent)

    # Event 26: MessageOutputItem run_item_stream_event
    assert events[26].type == "run_item_stream_event"
    assert events[26].name == "message_output_created"
    assert isinstance(events[26].item, MessageOutputItem)
