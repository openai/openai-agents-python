from typing import Any, cast

import pytest
from openai.types.responses.response_output_item import McpCall, McpListTools, McpListToolsTool

from agents import Agent, HostedMCPTool
from agents.items import MCPListToolsItem, ModelResponse, RunItem, ToolCallItem, TResponseInputItem
from agents.lifecycle import RunHooks
from agents.models.fake_id import FAKE_RESPONSES_ID
from agents.result import RunResultStreaming
from agents.run_config import ModelInputData, RunConfig
from agents.run_context import RunContextWrapper
from agents.run_internal.oai_conversation import OpenAIServerConversationTracker
from agents.run_internal.run_loop import get_new_response, run_single_turn_streamed
from agents.run_internal.tool_use_tracker import AgentToolUseTracker
from agents.stream_events import RunItemStreamEvent
from agents.usage import Usage

from .fake_model import FakeModel
from .test_responses import get_text_message


class DummyRunItem:
    """Minimal stand-in for RunItem with the attributes used by OpenAIServerConversationTracker."""

    def __init__(self, raw_item: dict[str, Any], type: str = "message") -> None:
        self.raw_item = raw_item
        self.type = type


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


def test_prepare_input_filters_items_seen_by_server_and_tool_calls() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv", previous_response_id=None)

    original_input: list[TResponseInputItem] = [
        cast(TResponseInputItem, {"id": "input-1", "type": "message"}),
        cast(TResponseInputItem, {"id": "input-2", "type": "message"}),
    ]
    new_raw_item = {"type": "message", "content": "hello"}
    generated_items = [
        DummyRunItem({"id": "server-echo", "type": "message"}),
        DummyRunItem(new_raw_item),
        DummyRunItem({"call_id": "call-1", "output": "done"}, type="function_call_output_item"),
    ]
    model_response = object.__new__(ModelResponse)
    model_response.output = [
        cast(Any, {"call_id": "call-1", "output": "prior", "type": "function_call_output"})
    ]
    model_response.usage = Usage()
    model_response.response_id = "resp-1"
    session_items: list[TResponseInputItem] = [
        cast(TResponseInputItem, {"id": "session-1", "type": "message"})
    ]

    tracker.hydrate_from_state(
        original_input=original_input,
        generated_items=cast(list[Any], generated_items),
        model_responses=[model_response],
        session_items=session_items,
    )

    prepared = tracker.prepare_input(
        original_input=original_input,
        generated_items=cast(list[Any], generated_items),
    )

    assert prepared == [new_raw_item]
    assert tracker.sent_initial_input is True
    assert tracker.remaining_initial_input is None


def test_hydrate_from_state_does_not_track_string_initial_input_by_object_identity() -> None:
    tracker = OpenAIServerConversationTracker(
        conversation_id="conv-init-string", previous_response_id=None
    )

    tracker.hydrate_from_state(
        original_input="hello",
        generated_items=[],
        model_responses=[],
    )

    assert tracker.sent_items == set()
    assert tracker.sent_initial_input is True
    assert tracker.remaining_initial_input is None
    assert len(tracker.sent_item_fingerprints) == 1


def test_hydrate_from_state_does_not_track_list_initial_input_by_object_identity() -> None:
    tracker = OpenAIServerConversationTracker(
        conversation_id="conv-init-list", previous_response_id=None
    )
    original_input = [cast(TResponseInputItem, {"role": "user", "content": "hello"})]

    tracker.hydrate_from_state(
        original_input=original_input,
        generated_items=[],
        model_responses=[],
    )

    assert tracker.sent_items == set()
    assert tracker.sent_initial_input is True
    assert tracker.remaining_initial_input is None
    assert len(tracker.sent_item_fingerprints) == 1


def test_mark_input_as_sent_and_rewind_input_respects_remaining_initial_input() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv2", previous_response_id=None)
    pending_1: TResponseInputItem = cast(TResponseInputItem, {"id": "p-1", "type": "message"})
    pending_2: TResponseInputItem = cast(TResponseInputItem, {"id": "p-2", "type": "message"})
    tracker.remaining_initial_input = [pending_1, pending_2]

    tracker.mark_input_as_sent(
        [pending_1, cast(TResponseInputItem, {"id": "p-2", "type": "message"})]
    )
    assert tracker.remaining_initial_input is None

    tracker.rewind_input([pending_1])
    assert tracker.remaining_initial_input == [pending_1]


def test_mark_input_as_sent_uses_raw_generated_source_for_rebuilt_filtered_item() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv2b", previous_response_id=None)
    raw_generated_item = {
        "type": "function_call_output",
        "call_id": "call-2b",
        "output": "done",
    }
    generated_items = [
        DummyRunItem(raw_generated_item, type="function_call_output_item"),
    ]

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], generated_items),
    )
    rebuilt_filtered_item = cast(TResponseInputItem, dict(cast(dict[str, Any], prepared[0])))

    tracker.mark_input_as_sent([rebuilt_filtered_item])

    assert id(raw_generated_item) in tracker.sent_items
    assert id(rebuilt_filtered_item) not in tracker.sent_items

    prepared_again = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], generated_items),
    )
    assert prepared_again == []


def test_hydrate_from_state_skips_restored_tool_search_items_by_object_identity() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv2c", previous_response_id=None)
    tool_search_call = {
        "type": "tool_search_call",
        "queries": [{"search_term": "account balance"}],
    }
    tool_search_result = {
        "type": "tool_search_output",
        "results": [{"text": "Balance lookup docs"}],
    }
    hydrated_items = [
        DummyRunItem(tool_search_call, type="tool_search_call_item"),
        DummyRunItem(tool_search_result, type="tool_search_output_item"),
    ]

    tracker.hydrate_from_state(
        original_input=[],
        generated_items=cast(list[Any], hydrated_items),
        model_responses=[],
    )

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], hydrated_items),
    )

    assert prepared == []


def test_hydrate_from_state_skips_restored_tool_search_items_by_fingerprint() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv2d", previous_response_id=None)
    tool_search_call = {
        "type": "tool_search_call",
        "queries": [{"search_term": "account balance"}],
    }
    tool_search_result = {
        "type": "tool_search_output",
        "results": [{"text": "Balance lookup docs"}],
    }
    hydrated_items = [
        DummyRunItem(tool_search_call, type="tool_search_call_item"),
        DummyRunItem(tool_search_result, type="tool_search_output_item"),
    ]
    rebuilt_items = [
        DummyRunItem(dict(tool_search_call), type="tool_search_call_item"),
        DummyRunItem(dict(tool_search_result), type="tool_search_output_item"),
    ]

    tracker.hydrate_from_state(
        original_input=[],
        generated_items=cast(list[Any], hydrated_items),
        model_responses=[],
    )

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], rebuilt_items),
    )

    assert prepared == []


def test_hydrate_from_state_skips_restored_tool_search_items_when_created_by_is_stripped() -> None:
    tracker = OpenAIServerConversationTracker(
        conversation_id="conv2d-created-by", previous_response_id=None
    )
    session_items = [
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "call_id": "tool_search_call_1",
                "arguments": {"query": "account balance"},
                "execution": "server",
                "status": "completed",
                "created_by": "server",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "call_id": "tool_search_call_1",
                "execution": "server",
                "status": "completed",
                "tools": [],
                "created_by": "server",
            },
        ),
    ]

    tracker.hydrate_from_state(
        original_input=[],
        generated_items=[],
        model_responses=[],
        session_items=session_items,
    )

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(
            list[RunItem],
            [
                DummyRunItem(
                    {
                        "type": "tool_search_call",
                        "call_id": "tool_search_call_1",
                        "arguments": {"query": "account balance"},
                        "execution": "server",
                        "status": "completed",
                    },
                    type="tool_search_call_item",
                ),
                DummyRunItem(
                    {
                        "type": "tool_search_output",
                        "call_id": "tool_search_call_1",
                        "execution": "server",
                        "status": "completed",
                        "tools": [],
                    },
                    type="tool_search_output_item",
                ),
            ],
        ),
    )

    assert prepared == []


def test_hydrate_from_state_skips_restored_tool_search_items_when_only_ids_differ() -> None:
    tracker = OpenAIServerConversationTracker(
        conversation_id="conv2d-ids-only", previous_response_id=None
    )
    session_items = [
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "id": "tool_search_call_saved",
                "arguments": {"query": "account balance"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "id": "tool_search_output_saved",
                "execution": "server",
                "status": "completed",
                "tools": [],
            },
        ),
    ]

    tracker.hydrate_from_state(
        original_input=[],
        generated_items=[],
        model_responses=[],
        session_items=session_items,
    )

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(
            list[RunItem],
            [
                DummyRunItem(
                    {
                        "type": "tool_search_call",
                        "arguments": {"query": "account balance"},
                        "execution": "server",
                        "status": "completed",
                    },
                    type="tool_search_call_item",
                ),
                DummyRunItem(
                    {
                        "type": "tool_search_output",
                        "execution": "server",
                        "status": "completed",
                        "tools": [],
                    },
                    type="tool_search_output_item",
                ),
            ],
        ),
    )

    assert prepared == []


def test_prepare_input_keeps_repeated_tool_search_items_with_new_ids() -> None:
    tracker = OpenAIServerConversationTracker(
        conversation_id="conv2d-repeated-search", previous_response_id=None
    )

    prior_response = object.__new__(ModelResponse)
    prior_response.output = [
        cast(
            Any,
            {
                "type": "tool_search_call",
                "id": "tool_search_call_saved",
                "arguments": {"query": "account balance"},
                "execution": "server",
                "status": "completed",
                "created_by": "server",
            },
        ),
        cast(
            Any,
            {
                "type": "tool_search_output",
                "id": "tool_search_output_saved",
                "execution": "server",
                "status": "completed",
                "tools": [],
                "created_by": "server",
            },
        ),
    ]
    prior_response.usage = Usage()
    prior_response.response_id = "resp-tool-search-repeat-1"

    tracker.track_server_items(prior_response)

    repeated_items = [
        DummyRunItem(
            {
                "type": "tool_search_call",
                "id": "tool_search_call_repeat",
                "arguments": {"query": "account balance"},
                "execution": "server",
                "status": "completed",
            },
            type="tool_search_call_item",
        ),
        DummyRunItem(
            {
                "type": "tool_search_output",
                "id": "tool_search_output_repeat",
                "execution": "server",
                "status": "completed",
                "tools": [],
            },
            type="tool_search_output_item",
        ),
    ]

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], repeated_items),
    )

    assert prepared == [
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "id": "tool_search_call_repeat",
                "arguments": {"query": "account balance"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_output",
                "id": "tool_search_output_repeat",
                "execution": "server",
                "status": "completed",
                "tools": [],
            },
        ),
    ]


def test_track_server_items_skips_live_tool_search_items_on_next_prepare() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv2e", previous_response_id=None)
    tool_search_call = cast(
        Any,
        {
            "type": "tool_search_call",
            "call_id": "tool_search_call_live",
            "arguments": {"query": "account balance"},
            "execution": "server",
            "status": "completed",
            "created_by": "server",
        },
    )
    tool_search_result = cast(
        Any,
        {
            "type": "tool_search_output",
            "call_id": "tool_search_call_live",
            "execution": "server",
            "status": "completed",
            "tools": [],
            "created_by": "server",
        },
    )
    model_response = object.__new__(ModelResponse)
    model_response.output = [tool_search_call, tool_search_result]
    model_response.usage = Usage()
    model_response.response_id = "resp-tool-search"

    tracker.track_server_items(model_response)

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(
            list[RunItem],
            [
                DummyRunItem(
                    {
                        "type": "tool_search_call",
                        "call_id": "tool_search_call_live",
                        "arguments": {"query": "account balance"},
                        "execution": "server",
                        "status": "completed",
                    },
                    type="tool_search_call_item",
                ),
                DummyRunItem(
                    {
                        "type": "tool_search_output",
                        "call_id": "tool_search_call_live",
                        "execution": "server",
                        "status": "completed",
                        "tools": [],
                    },
                    type="tool_search_output_item",
                ),
            ],
        ),
    )

    assert prepared == []


def test_track_server_items_filters_pending_tool_search_by_sanitized_fingerprint() -> None:
    tracker = OpenAIServerConversationTracker(
        conversation_id="conv2e-pending", previous_response_id=None
    )
    tracker.remaining_initial_input = [
        cast(
            TResponseInputItem,
            {
                "type": "tool_search_call",
                "call_id": "tool_search_pending",
                "arguments": {"query": "account balance"},
                "execution": "server",
                "status": "completed",
            },
        ),
        cast(TResponseInputItem, {"id": "keep-me", "type": "message"}),
    ]

    model_response = object.__new__(ModelResponse)
    model_response.output = [
        cast(
            Any,
            {
                "type": "tool_search_call",
                "call_id": "tool_search_pending",
                "arguments": {"query": "account balance"},
                "execution": "server",
                "status": "completed",
                "created_by": "server",
            },
        )
    ]
    model_response.usage = Usage()
    model_response.response_id = "resp-tool-search-pending"

    tracker.track_server_items(model_response)

    assert tracker.remaining_initial_input == [
        cast(TResponseInputItem, {"id": "keep-me", "type": "message"})
    ]


def test_track_server_items_filters_remaining_initial_input_by_fingerprint() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv3", previous_response_id=None)
    pending_kept: TResponseInputItem = cast(
        TResponseInputItem, {"id": "keep-me", "type": "message"}
    )
    pending_filtered: TResponseInputItem = cast(
        TResponseInputItem,
        {"type": "function_call_output", "call_id": "call-2", "output": "x"},
    )
    tracker.remaining_initial_input = [pending_kept, pending_filtered]

    model_response = object.__new__(ModelResponse)
    model_response.output = [
        cast(Any, {"type": "function_call_output", "call_id": "call-2", "output": "x"})
    ]
    model_response.usage = Usage()
    model_response.response_id = "resp-2"

    tracker.track_server_items(model_response)

    assert tracker.remaining_initial_input == [pending_kept]


def test_prepare_input_does_not_skip_fake_response_ids() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv5", previous_response_id=None)

    model_response = object.__new__(ModelResponse)
    model_response.output = [cast(Any, {"id": FAKE_RESPONSES_ID, "type": "message"})]
    model_response.usage = Usage()
    model_response.response_id = "resp-3"

    tracker.track_server_items(model_response)

    raw_item = {"id": FAKE_RESPONSES_ID, "type": "message", "content": "hello"}
    generated_items = [DummyRunItem(raw_item)]

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], generated_items),
    )

    assert prepared == [raw_item]


def test_prepare_input_applies_reasoning_item_id_policy_for_generated_items() -> None:
    tracker = OpenAIServerConversationTracker(
        conversation_id="conv7",
        previous_response_id=None,
        reasoning_item_id_policy="omit",
    )
    generated_items = [
        DummyRunItem(
            {
                "type": "reasoning",
                "id": "rs_turn_input",
                "content": [{"type": "input_text", "text": "reasoning trace"}],
            },
            type="reasoning_item",
        )
    ]

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], generated_items),
    )

    assert prepared == [
        cast(
            TResponseInputItem,
            {"type": "reasoning", "content": [{"type": "input_text", "text": "reasoning trace"}]},
        )
    ]


def test_prepare_input_does_not_resend_reasoning_item_after_marking_omitted_id_as_sent() -> None:
    tracker = OpenAIServerConversationTracker(
        conversation_id="conv8",
        previous_response_id=None,
        reasoning_item_id_policy="omit",
    )
    generated_items = [
        DummyRunItem(
            {
                "type": "reasoning",
                "id": "rs_turn_input",
                "content": [{"type": "input_text", "text": "reasoning trace"}],
            },
            type="reasoning_item",
        )
    ]

    first_prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], generated_items),
    )
    assert first_prepared == [
        cast(
            TResponseInputItem,
            {"type": "reasoning", "content": [{"type": "input_text", "text": "reasoning trace"}]},
        )
    ]

    tracker.mark_input_as_sent(first_prepared)

    second_prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], generated_items),
    )
    assert second_prepared == []


@pytest.mark.asyncio
async def test_get_new_response_marks_filtered_input_as_sent() -> None:
    model = FakeModel()
    model.set_next_output([get_text_message("ok")])
    agent = Agent(name="test", model=model)
    tracker = OpenAIServerConversationTracker(conversation_id="conv4", previous_response_id=None)
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})
    tool_use_tracker = AgentToolUseTracker()

    item_1: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "first"})
    item_2: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "second"})

    def _filter_input(payload: Any) -> ModelInputData:
        return ModelInputData(
            input=[payload.model_data.input[0]],
            instructions=payload.model_data.instructions,
        )

    run_config = RunConfig(call_model_input_filter=_filter_input)

    await get_new_response(
        agent,
        None,
        [item_1, item_2],
        None,
        [],
        [],
        RunHooks(),
        context_wrapper,
        run_config,
        tool_use_tracker,
        tracker,
        None,
    )

    assert model.last_turn_args["input"] == [item_1]
    assert id(item_1) in tracker.sent_items
    assert id(item_2) not in tracker.sent_items


@pytest.mark.asyncio
async def test_run_single_turn_streamed_marks_filtered_input_as_sent() -> None:
    model = FakeModel()
    model.set_next_output([get_text_message("ok")])
    agent = Agent(name="test", model=model)
    tracker = OpenAIServerConversationTracker(conversation_id="conv6", previous_response_id=None)
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})
    tool_use_tracker = AgentToolUseTracker()

    item_1: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "first"})
    item_2: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "second"})

    def _filter_input(payload: Any) -> ModelInputData:
        return ModelInputData(
            input=[payload.model_data.input[0]],
            instructions=payload.model_data.instructions,
        )

    run_config = RunConfig(call_model_input_filter=_filter_input)

    streamed_result = RunResultStreaming(
        input=[item_1, item_2],
        new_items=[],
        raw_responses=[],
        final_output=None,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=context_wrapper,
        current_agent=agent,
        current_turn=0,
        max_turns=1,
        _current_agent_output_schema=None,
        trace=None,
        interruptions=[],
    )

    await run_single_turn_streamed(
        streamed_result,
        agent,
        RunHooks(),
        context_wrapper,
        run_config,
        should_run_agent_start_hooks=False,
        tool_use_tracker=tool_use_tracker,
        all_tools=[],
        server_conversation_tracker=tracker,
    )

    assert model.last_turn_args["input"] == [item_1]
    assert tracker.remaining_initial_input == [item_2]


@pytest.mark.asyncio
async def test_run_single_turn_streamed_seeds_hosted_mcp_metadata_from_pre_step_items() -> None:
    model = FakeModel()
    mcp_call = McpCall(
        id="mcp_call_1",
        arguments="{}",
        name="search_docs",
        server_label="docs_server",
        type="mcp_call",
        status="completed",
    )
    model.set_next_output([mcp_call])
    agent = Agent(name="test", model=model)
    hosted_tool = HostedMCPTool(
        tool_config=cast(
            Any,
            {
                "type": "mcp",
                "server_label": "docs_server",
                "server_url": "https://example.com/mcp",
            },
        )
    )
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})
    tool_use_tracker = AgentToolUseTracker()

    item_1: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "first"})
    pre_step_item = MCPListToolsItem(
        agent=agent,
        raw_item=_make_hosted_mcp_list_tools("docs_server", "search_docs"),
    )

    def _filter_input(payload: Any) -> ModelInputData:
        return ModelInputData(
            input=[payload.model_data.input[0]],
            instructions=payload.model_data.instructions,
        )

    run_config = RunConfig(call_model_input_filter=_filter_input)

    streamed_result = RunResultStreaming(
        input=[item_1],
        new_items=[],
        raw_responses=[],
        final_output=None,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=context_wrapper,
        current_agent=agent,
        current_turn=1,
        max_turns=2,
        _current_agent_output_schema=None,
        trace=None,
        interruptions=[],
    )
    streamed_result._model_input_items = [pre_step_item]

    await run_single_turn_streamed(
        streamed_result,
        agent,
        RunHooks(),
        context_wrapper,
        run_config,
        should_run_agent_start_hooks=False,
        tool_use_tracker=tool_use_tracker,
        all_tools=[hosted_tool],
    )

    assert model.last_turn_args["input"] == [item_1]

    tool_call_events: list[ToolCallItem] = []
    while not streamed_result._event_queue.empty():
        queued_event = streamed_result._event_queue.get_nowait()
        streamed_result._event_queue.task_done()
        if (
            isinstance(queued_event, RunItemStreamEvent)
            and queued_event.name == "tool_called"
            and isinstance(queued_event.item, ToolCallItem)
        ):
            tool_call_events.append(queued_event.item)

    assert len(tool_call_events) == 1
    assert tool_call_events[0].description == "Search the docs."
    assert tool_call_events[0].title == "Search Docs"
