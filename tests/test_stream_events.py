import asyncio
import time
from copy import deepcopy
from typing import Any, cast

import pytest
from openai._models import construct_type
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseInProgressEvent,
    ResponseOutputItem,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningSummaryPartDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningSummaryTextDoneEvent,
    ResponseTextDeltaEvent,
    ResponseTextDoneEvent,
    ResponseToolSearchCall,
    ResponseToolSearchOutputItem,
)
from openai.types.responses.response_output_item import (
    McpApprovalRequest,
    McpListTools,
    McpListToolsTool,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary

from agents import Agent, HandoffCallItem, Runner, function_tool
from agents.extensions.handoff_filters import nest_handoff_history, remove_all_tools
from agents.handoffs import HandoffInputData, handoff
from agents.items import (
    CompactionItem,
    ItemHelpers,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MCPListToolsItem,
    MessageOutputItem,
    ReasoningItem,
    RunItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    ToolSearchCallItem,
    ToolSearchOutputItem,
)
from agents.run_internal.streaming import stream_step_items_to_queue, stream_step_result_to_queue

from .fake_model import FakeModel
from .mcp.helpers import FakeMCPServer
from .mcp.model_compat import Tool as MCPTool
from .test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message


def get_reasoning_item() -> ResponseReasoningItem:
    return ResponseReasoningItem(
        id="rid", type="reasoning", summary=[Summary(text="thinking", type="summary_text")]
    )


def _make_hosted_mcp_list_tools(server_label: str, tool_name: str) -> McpListTools:
    return McpListTools(
        id=f"list_{server_label}",
        server_label=server_label,
        tools=[
            McpListToolsTool(
                name=tool_name,
                input_schema={},
                description="Search the docs.",
                annotations={"title": "Search Docs"},
            )
        ],
        type="mcp_list_tools",
    )


@function_tool
async def foo() -> str:
    await asyncio.sleep(0)
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
async def test_stream_events_tool_called_includes_local_mcp_title() -> None:
    model = FakeModel()
    server = FakeMCPServer(
        tools=[
            MCPTool(
                name="search_docs",
                inputSchema={},
                description=None,
                title="Search Docs",
            )
        ]
    )
    agent = Agent(name="MCPAgent", model=model, mcp_servers=[server])

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("search_docs", "{}")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="Hello")
    seen_tool_item: ToolCallItem | None = None
    async for event in result.stream_events():
        if (
            event.type == "run_item_stream_event"
            and isinstance(event.item, ToolCallItem)
            and seen_tool_item is None
        ):
            seen_tool_item = event.item

    assert seen_tool_item is not None
    assert seen_tool_item.description == "Search Docs"
    assert seen_tool_item.title == "Search Docs"


def test_stream_step_items_to_queue_emits_helper_events_and_skips_approvals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = Agent(name="StreamHelper")
    queue: asyncio.Queue[Any] = asyncio.Queue()
    request_item = McpApprovalRequest(
        id="mcp-approval-1",
        type="mcp_approval_request",
        server_label="test-mcp-server",
        arguments="{}",
        name="search_docs",
    )

    items: list[RunItem] = [
        ToolSearchCallItem(
            agent=agent,
            raw_item=ResponseToolSearchCall(
                id="tsc_123",
                type="tool_search_call",
                arguments={"query": "docs"},
                execution="client",
                status="completed",
            ),
        ),
        ToolSearchOutputItem(
            agent=agent,
            raw_item=ResponseToolSearchOutputItem(
                id="tso_123",
                type="tool_search_output",
                execution="client",
                status="completed",
                tools=[],
            ),
        ),
        MCPApprovalRequestItem(agent=agent, raw_item=request_item),
        MCPApprovalResponseItem(
            agent=agent,
            raw_item=cast(
                Any,
                {
                    "type": "mcp_approval_response",
                    "approval_request_id": "mcp-approval-1",
                    "approve": True,
                },
            ),
        ),
        MCPListToolsItem(
            agent=agent,
            raw_item=_make_hosted_mcp_list_tools("test-mcp-server", "search_docs"),
        ),
        ReasoningItem(agent=agent, raw_item=get_reasoning_item()),
        ToolApprovalItem(
            agent=agent,
            raw_item={"type": "function_call", "call_id": "call-1", "name": "tool"},
        ),
        cast(Any, object()),
    ]

    with caplog.at_level("WARNING", logger="openai.agents"):
        stream_step_items_to_queue(items, queue)

    names = []
    while not queue.empty():
        event = queue.get_nowait()
        names.append(event.name)

    assert names == [
        "tool_search_called",
        "tool_search_output_created",
        "mcp_approval_requested",
        "mcp_approval_response",
        "mcp_list_tools",
        "reasoning_item_created",
    ]
    assert "Unexpected item type" in caplog.text


def test_stream_step_items_to_queue_skips_compaction_items_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CompactionItem is a session-bookkeeping RunItem with no public stream
    event name; it must be skipped silently rather than logged as unexpected."""
    agent = Agent(name="StreamHelper")
    queue: asyncio.Queue[Any] = asyncio.Queue()

    compaction_item = CompactionItem(
        agent=agent,
        raw_item=cast(Any, {"type": "compaction", "summary": "compacted"}),
    )

    with caplog.at_level("WARNING", logger="openai.agents"):
        stream_step_items_to_queue([compaction_item], queue)

    assert queue.empty()
    assert "Unexpected item type" not in caplog.text


def test_stream_step_result_to_queue_uses_new_step_items() -> None:
    agent = Agent(name="StreamHelper")
    queue: asyncio.Queue[Any] = asyncio.Queue()

    tool_search_item = ToolSearchCallItem(
        agent=agent,
        raw_item={
            "type": "tool_search_call",
            "queries": [{"search_term": "docs"}],
        },
    )
    step_result = cast(Any, type("StepResult", (), {"new_step_items": [tool_search_item]})())

    stream_step_result_to_queue(step_result, queue)

    event = queue.get_nowait()
    assert event.name == "tool_search_called"


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

    # Event 9: ResponseOutputItemAddedEvent (function call)
    assert events[9].type == "raw_response_event"
    assert isinstance(events[9].data, ResponseOutputItemAddedEvent)

    # Event 10: ResponseFunctionCallArgumentsDeltaEvent
    assert events[10].type == "raw_response_event"
    assert isinstance(events[10].data, ResponseFunctionCallArgumentsDeltaEvent)

    # Event 11: ResponseFunctionCallArgumentsDoneEvent
    assert events[11].type == "raw_response_event"
    assert isinstance(events[11].data, ResponseFunctionCallArgumentsDoneEvent)

    # Event 12: ResponseOutputItemDoneEvent (function call)
    assert events[12].type == "raw_response_event"
    assert isinstance(events[12].data, ResponseOutputItemDoneEvent)

    # Event 13: ResponseCompletedEvent (first turn ended)
    assert events[13].type == "raw_response_event"
    assert isinstance(events[13].data, ResponseCompletedEvent)

    # Event 14: ReasoningItem after the complete response passes canonical validation
    assert events[14].type == "run_item_stream_event"
    assert events[14].name == "reasoning_item_created"
    assert isinstance(events[14].item, ReasoningItem)

    # Event 15: ToolCallItem after the complete response passes canonical validation
    assert events[15].type == "run_item_stream_event"
    assert events[15].name == "tool_called"
    assert isinstance(events[15].item, ToolCallItem)

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


@pytest.mark.asyncio
async def test_tool_call_event_preserves_order_before_later_reasoning_item() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("foo", '{"arg": "value"}'),
                get_reasoning_item(),
            ],
            [get_text_message("Final response")],
        ]
    )
    agent = Agent(name="TestAgent", model=model, tools=[foo])

    result = Runner.run_streamed(agent, input="Hello")
    semantic_event_names = [
        event.name
        async for event in result.stream_events()
        if event.type == "run_item_stream_event"
    ]

    assert semantic_event_names[:3] == [
        "tool_called",
        "reasoning_item_created",
        "tool_output",
    ]


@pytest.mark.asyncio
async def test_handoff_event_preserves_order_before_later_reasoning_item() -> None:
    english_agent = Agent(
        name="EnglishAgent",
        model=FakeModel(initial_output=[get_text_message("Done")]),
    )
    model = FakeModel(
        initial_output=[
            get_handoff_tool_call(english_agent),
            get_reasoning_item(),
        ]
    )
    triage_agent = Agent(name="TriageAgent", model=model, handoffs=[english_agent])

    result = Runner.run_streamed(triage_agent, input="Start")
    semantic_event_names = [
        event.name
        async for event in result.stream_events()
        if event.type == "run_item_stream_event"
    ]

    assert semantic_event_names[:2] == [
        "handoff_requested",
        "reasoning_item_created",
    ]


@pytest.mark.asyncio
async def test_handoff_filter_copy_does_not_duplicate_streamed_model_items() -> None:
    def copied_filter(data: HandoffInputData) -> HandoffInputData:
        nested = nest_handoff_history(data)
        return nested.clone(new_items=deepcopy(nested.new_items))

    english_agent = Agent(
        name="EnglishAgent",
        model=FakeModel(initial_output=[get_text_message("Done")]),
    )
    model = FakeModel(
        initial_output=[
            get_text_message("Transferring"),
            get_handoff_tool_call(english_agent),
        ]
    )
    triage_agent = Agent(
        name="TriageAgent",
        model=model,
        handoffs=[handoff(english_agent, input_filter=copied_filter)],
    )

    result = Runner.run_streamed(triage_agent, input="Start")
    item_events = [
        event async for event in result.stream_events() if event.type == "run_item_stream_event"
    ]

    message_texts = [
        ItemHelpers.text_message_output(event.item)
        for event in item_events
        if isinstance(event.item, MessageOutputItem)
    ]
    assert message_texts == ["Transferring", "Done"]
    assert sum(event.name == "handoff_requested" for event in item_events) == 1


@pytest.mark.asyncio
async def test_stream_events_emit_tool_search_items() -> None:
    model = FakeModel()
    agent = Agent(name="ToolSearchAgent", model=model)
    tool_search_call = cast(
        ResponseOutputItem,
        construct_type(
            type_=ResponseOutputItem,
            value={
                "id": "tsc_stream",
                "type": "tool_search_call",
                "arguments": {"paths": ["crm"], "query": "orders"},
                "execution": "server",
                "status": "completed",
            },
        ),
    )
    tool_search_output = cast(
        ResponseOutputItem,
        construct_type(
            type_=ResponseOutputItem,
            value={
                "id": "tso_stream",
                "type": "tool_search_output",
                "execution": "server",
                "status": "completed",
                "tools": [
                    {
                        "type": "function",
                        "name": "list_open_orders",
                        "description": "List open orders for a customer.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "customer_id": {
                                    "type": "string",
                                }
                            },
                            "required": ["customer_id"],
                        },
                        "defer_loading": True,
                    }
                ],
            },
        ),
    )
    model.add_multiple_turn_outputs(
        [[tool_search_call, tool_search_output, get_text_message("Done")]]
    )

    result = Runner.run_streamed(agent, input="Search for CRM order tools")

    seen_events: list[tuple[str, object]] = []
    async for event in result.stream_events():
        if event.type != "run_item_stream_event":
            continue
        seen_events.append((event.name, event.item))

    assert any(
        name == "tool_search_called" and isinstance(item, ToolSearchCallItem)
        for name, item in seen_events
    )
    assert any(
        name == "tool_search_output_created" and isinstance(item, ToolSearchOutputItem)
        for name, item in seen_events
    )


@pytest.mark.asyncio
async def test_streamed_handoff_call_is_not_emitted_as_tool_called():
    """A handoff call streams only as `handoff_requested`, never also as `tool_called`."""
    english_agent = Agent(name="EnglishAgent", model=FakeModel())

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_handoff_tool_call(english_agent)],
            [get_text_message("Done")],
        ]
    )
    triage_agent = Agent(name="TriageAgent", handoffs=[english_agent], model=model)

    result = Runner.run_streamed(triage_agent, input="Start")

    item_events = [
        (event.name, event.item)
        async for event in result.stream_events()
        if event.type == "run_item_stream_event"
    ]

    handoff_events = [
        (name, item) for name, item in item_events if isinstance(item, HandoffCallItem)
    ]
    assert len(handoff_events) == 1
    assert handoff_events[0][0] == "handoff_requested"

    assert [name for name, _ in item_events if name == "tool_called"] == []
    assert not any(isinstance(item, ToolCallItem) for _, item in item_events)


@pytest.mark.asyncio
async def test_streamed_tool_call_alongside_handoff_still_emits_tool_called():
    """A real tool call in the same turn as a handoff keeps its `tool_called` event."""
    english_agent = Agent(name="EnglishAgent", model=FakeModel())

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("foo", '{"a": "b"}', call_id="tool_call"),
                get_handoff_tool_call(english_agent),
            ],
            [get_text_message("Done")],
        ]
    )
    triage_agent = Agent(
        name="TriageAgent",
        handoffs=[handoff(english_agent, input_filter=remove_all_tools)],
        tools=[foo],
        model=model,
    )

    result = Runner.run_streamed(triage_agent, input="Start")

    item_events = [
        (event.name, event.item)
        async for event in result.stream_events()
        if event.type == "run_item_stream_event"
    ]

    tool_called_items = [item for name, item in item_events if name == "tool_called"]
    assert len(tool_called_items) == 1
    assert cast(ToolCallItem, tool_called_items[0]).call_id == "tool_call"

    assert [name for name, item in item_events if isinstance(item, HandoffCallItem)] == [
        "handoff_requested"
    ]


@pytest.mark.asyncio
async def test_streamed_handoff_item_events_match_new_items():
    """Streamed run item events stay in sync with the items recorded on the result."""
    english_agent = Agent(name="EnglishAgent", model=FakeModel())

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("Transferring"), get_handoff_tool_call(english_agent)],
            [get_text_message("Done")],
        ]
    )
    triage_agent = Agent(name="TriageAgent", handoffs=[english_agent], model=model)

    result = Runner.run_streamed(triage_agent, input="Start")

    streamed_item_types = [
        event.item.type
        async for event in result.stream_events()
        if event.type == "run_item_stream_event"
    ]

    assert sorted(streamed_item_types) == sorted(item.type for item in result.new_items)
