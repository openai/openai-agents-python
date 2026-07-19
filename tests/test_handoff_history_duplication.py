"""Tests for handoff history duplication fix (Issue #2171).

These tests verify that when nest_handoff_history is enabled,
function_call and function_call_output items are NOT duplicated
in the input sent to the next agent.
"""

import dataclasses
import json
from copy import deepcopy
from typing import Any, cast

import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseToolSearchCall,
    ResponseToolSearchOutputItem,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary

from agents import (
    Agent,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    RunState,
    function_tool,
    handoff,
)
from agents.extensions.handoff_filters import remove_all_tools
from agents.handoffs import (
    HandoffInputData,
    default_handoff_history_mapper,
    nest_handoff_history,
    reset_conversation_history_wrappers,
    set_conversation_history_wrappers,
)
from agents.handoffs.history import _get_nested_history_owned_items
from agents.items import (
    HandoffCallItem,
    HandoffOutputItem,
    MessageOutputItem,
    ReasoningItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from agents.result import RunResult, RunResultStreaming
from agents.run_internal.items import (
    NestedHistoryOwnedItem,
    digest_input_item,
    nested_history_run_item_occurrence_key,
)
from agents.run_internal.session_persistence import (
    resolve_nested_history_owned_session_item_refs,
)

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message
from .utils.simple_session import SimpleListSession


def _create_mock_agent() -> Agent:
    """Create a mock agent for testing."""
    return Agent(name="test_agent")


def _create_tool_call_item(agent: Agent) -> ToolCallItem:
    """Create a mock ToolCallItem."""
    raw_item = ResponseFunctionToolCall(
        id="call_tool_123",
        call_id="call_tool_123",
        name="get_weather",
        arguments='{"city": "London"}',
        type="function_call",
    )
    return ToolCallItem(agent=agent, raw_item=raw_item, type="tool_call_item")


def _create_tool_output_item(agent: Agent) -> ToolCallOutputItem:
    """Create a mock ToolCallOutputItem."""
    raw_item = {
        "type": "function_call_output",
        "call_id": "call_tool_123",
        "output": "Sunny, 22°C",
    }
    return ToolCallOutputItem(
        agent=agent,
        raw_item=raw_item,
        output="Sunny, 22°C",
        type="tool_call_output_item",
    )


def _create_handoff_call_item(agent: Agent) -> HandoffCallItem:
    """Create a mock HandoffCallItem."""
    raw_item = ResponseFunctionToolCall(
        id="call_handoff_456",
        call_id="call_handoff_456",
        name="transfer_to_agent_b",
        arguments="{}",
        type="function_call",
    )
    return HandoffCallItem(agent=agent, raw_item=raw_item, type="handoff_call_item")


def _create_handoff_output_item(agent: Agent[Any]) -> HandoffOutputItem:
    """Create a mock HandoffOutputItem."""
    raw_item: dict[str, str] = {
        "type": "function_call_output",
        "call_id": "call_handoff_456",
        "output": '{"assistant": "agent_b"}',
    }
    return HandoffOutputItem(
        agent=agent,
        raw_item=cast(Any, raw_item),
        source_agent=agent,
        target_agent=agent,
        type="handoff_output_item",
    )


def _create_message_item(agent: Agent, *, text: str = "Hello!") -> MessageOutputItem:
    """Create a mock MessageOutputItem."""
    raw_item = ResponseOutputMessage(
        id="msg_123",
        content=[ResponseOutputText(text=text, type="output_text", annotations=[])],
        role="assistant",
        status="completed",
        type="message",
    )
    return MessageOutputItem(agent=agent, raw_item=raw_item, type="message_output_item")


def _input_item_text(item: TResponseInputItem) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _create_reasoning_item(agent: Agent) -> ReasoningItem:
    """Create a mock ReasoningItem."""
    raw_item = ResponseReasoningItem(
        id="reasoning_123",
        type="reasoning",
        summary=[Summary(text="Thinking about handoff", type="summary_text")],
    )
    return ReasoningItem(agent=agent, raw_item=raw_item, type="reasoning_item")


def _create_tool_approval_item(agent: Agent) -> ToolApprovalItem:
    """Create a mock ToolApprovalItem."""
    raw_item = {
        "type": "function_call",
        "call_id": "call_tool_approve",
        "name": "needs_approval",
        "arguments": "{}",
    }
    return ToolApprovalItem(agent=agent, raw_item=raw_item)


class TestHandoffHistoryDuplicationFix:
    """Tests for Issue #2171: nest_handoff_history duplication fix."""

    def test_pre_handoff_tool_items_are_filtered(self):
        """Verify ToolCallItem and ToolCallOutputItem in pre_handoff_items are filtered.

        These items should NOT appear in the filtered output because they are
        already included in the summary message.
        """
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(
                _create_tool_call_item(agent),
                _create_tool_output_item(agent),
            ),
            new_items=(),
        )

        nested = nest_handoff_history(handoff_data)

        # pre_handoff_items should be empty (tool items filtered)
        assert len(nested.pre_handoff_items) == 0, (
            "ToolCallItem and ToolCallOutputItem should be filtered from pre_handoff_items"
        )

        # Summary should contain the conversation
        assert len(nested.input_history) == 1
        first_item = nested.input_history[0]
        assert isinstance(first_item, dict)
        assert "<CONVERSATION HISTORY>" in str(first_item.get("content", ""))

    def test_tool_approval_items_are_skipped(self):
        """Verify ToolApprovalItem does not break handoff history mapping."""
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(_create_tool_approval_item(agent),),
            new_items=(),
        )

        nested = nest_handoff_history(handoff_data)

        assert isinstance(nested.input_history, tuple)
        assert len(nested.pre_handoff_items) == 0
        assert nested.input_items == ()

    def test_pre_handoff_reasoning_items_are_filtered(self):
        """Verify ReasoningItem in pre_handoff_items is filtered.

        Reasoning is represented in the summary transcript and should not be
        forwarded as a raw item.
        """
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(_create_reasoning_item(agent),),
            new_items=(),
        )

        nested = nest_handoff_history(handoff_data)

        assert len(nested.pre_handoff_items) == 0
        first_item = nested.input_history[0]
        assert isinstance(first_item, dict)
        summary = str(first_item.get("content", ""))
        assert "reasoning" in summary

    def test_new_items_handoff_output_is_filtered_for_input(self):
        """Verify HandoffOutputItem in new_items is filtered from input_items.

        The HandoffOutputItem is a function_call_output which would be duplicated.
        It should be filtered from input_items but preserved in new_items.
        """
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(),
            new_items=(
                _create_handoff_call_item(agent),
                _create_handoff_output_item(agent),
            ),
        )

        nested = nest_handoff_history(handoff_data)

        # new_items should still have both items (for session history)
        assert len(nested.new_items) == 2, "new_items should preserve all items for session history"

        # input_items should be populated and filtered
        assert nested.input_items is not None, "input_items should be populated"

        # input_items should NOT contain HandoffOutputItem (it's function_call_output)
        has_handoff_output = any(isinstance(item, HandoffOutputItem) for item in nested.input_items)
        assert not has_handoff_output, "HandoffOutputItem should be filtered from input_items"

    def test_message_items_are_preserved_in_new_items(self):
        """Verify MessageOutputItem in new_items is preserved.

        Message items have a role and should be forwarded losslessly in input_history.
        """
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(),  # pre_handoff items go into summary
            new_items=(_create_message_item(agent),),
        )

        nested = nest_handoff_history(handoff_data)

        assert len(nested.new_items) == 1, "MessageOutputItem should be preserved in new_items"
        assert nested.input_items == ()
        assert len(nested.input_history) == 2
        raw_message = cast(dict[str, Any], nested.input_history[1])
        assert raw_message["role"] == "assistant"
        assert "Hello!" in str(raw_message["content"])

    def test_forwarded_items_are_excluded_from_summary_until_the_next_handoff(self):
        """A raw continuation item should have exactly one history owner at a time."""
        agent = _create_mock_agent()
        message_item = _create_message_item(agent)
        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(),
            new_items=(message_item,),
        )

        first_nested = nest_handoff_history(handoff_data)

        first_summary = str(cast(dict[str, Any], first_nested.input_history[0])["content"])
        assert "Hello!" not in first_summary
        assert first_nested.input_items == ()
        assert len(first_nested.input_history) == 2
        forwarded_message = cast(dict[str, Any], first_nested.input_history[1])
        assert forwarded_message["role"] == "assistant"
        assert "_agents_nested_history_token" not in forwarded_message

        second_nested = nest_handoff_history(
            HandoffInputData(
                input_history=first_nested.input_history,
                pre_handoff_items=(),
                new_items=(),
            )
        )
        summary = str(cast(dict[str, Any], second_nested.input_history[0])["content"])
        assert summary.count("Hello!") == 1
        assert "_agents_nested_history_token" not in summary
        assert second_nested.pre_handoff_items == ()

    def test_custom_mapper_receives_clean_flattened_history(self):
        """A later custom mapper must not receive SDK-only provenance metadata."""
        agent = _create_mock_agent()
        first_nested = nest_handoff_history(
            HandoffInputData(
                input_history=({"role": "user", "content": "Hello"},),
                pre_handoff_items=(),
                new_items=(_create_message_item(agent),),
            )
        )
        captured: list[TResponseInputItem] = []

        def capture_mapper(transcript: list[TResponseInputItem]) -> list[TResponseInputItem]:
            captured.extend(deepcopy(transcript))
            return transcript

        second_nested = nest_handoff_history(
            HandoffInputData(
                input_history=first_nested.input_history,
                pre_handoff_items=(),
                new_items=(),
            ),
            history_mapper=capture_mapper,
        )

        assert captured
        assert all("_agents_nested_history_token" not in item for item in captured)
        assert all(
            "_agents_nested_history_token" not in item for item in second_nested.input_history
        )

    def test_summary_and_raw_items_preserve_chronological_order(self):
        """Summary segments should not reorder interleaved lossless items."""
        agent = _create_mock_agent()
        data = HandoffInputData(
            input_history=({"role": "user", "content": "question"},),
            pre_handoff_items=(),
            new_items=(
                _create_message_item(agent, text="answer"),
                _create_handoff_call_item(agent),
                _create_handoff_output_item(agent),
            ),
        )

        nested = nest_handoff_history(data)

        assert len(nested.input_history) == 3
        prior_summary = str(cast(dict[str, Any], nested.input_history[0])["content"])
        raw_message = cast(dict[str, Any], nested.input_history[1])
        handoff_summary = str(cast(dict[str, Any], nested.input_history[2])["content"])
        assert "question" in prior_summary
        assert "answer" in str(raw_message["content"])
        assert "function_call" in handoff_summary
        assert "answer" not in prior_summary
        assert "answer" not in handoff_summary

    def test_custom_mapper_return_value_is_exact_model_input(self):
        """Nesting should not append SDK-selected items after a custom mapper."""
        agent = _create_mock_agent()
        mapped_item = cast(TResponseInputItem, {"role": "user", "content": "mapped"})
        data = HandoffInputData(
            input_history=({"role": "user", "content": "original"},),
            pre_handoff_items=(_create_message_item(agent, text="before"),),
            new_items=(_create_message_item(agent, text="after"),),
        )

        nested = nest_handoff_history(data, history_mapper=lambda _: [mapped_item])

        assert nested.input_history == (mapped_item,)
        assert nested.pre_handoff_items == ()
        assert nested.input_items == ()
        assert nested.new_items == data.new_items

    def test_reasoning_items_are_filtered_from_input_items(self):
        """Verify ReasoningItem in new_items is filtered from input_items.

        Reasoning is summarized in the conversation transcript and should not be
        forwarded verbatim in nested handoff model input.
        """
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(),
            new_items=(
                _create_reasoning_item(agent),
                _create_handoff_call_item(agent),
                _create_handoff_output_item(agent),
            ),
        )

        nested = nest_handoff_history(handoff_data)

        assert nested.input_items is not None
        has_reasoning = any(isinstance(item, ReasoningItem) for item in nested.input_items)
        assert not has_reasoning, "ReasoningItem should be filtered from input_items"

        first_item = nested.input_history[0]
        assert isinstance(first_item, dict)
        summary = str(first_item.get("content", ""))
        assert "reasoning" in summary

    def test_summary_contains_filtered_items_as_text(self):
        """Verify the summary message contains the filtered tool items as text.

        This ensures observability - the items are not lost, just converted to text.
        """
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(
                _create_tool_call_item(agent),
                _create_tool_output_item(agent),
            ),
            new_items=(),
        )

        nested = nest_handoff_history(handoff_data)

        first_item = nested.input_history[0]
        assert isinstance(first_item, dict)
        summary = str(first_item.get("content", ""))

        # Summary should contain function_call reference
        assert "function_call" in summary or "get_weather" in summary, (
            "Summary should contain the tool call that was filtered"
        )

    def test_input_items_field_exists_after_nesting(self):
        """Verify the input_items field is populated after nest_handoff_history.

        This is the key field that separates model input from session history.
        """
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(),
            new_items=(_create_handoff_call_item(agent),),
        )

        nested = nest_handoff_history(handoff_data)

        assert nested.input_items is not None, (
            "input_items should be populated after nest_handoff_history"
        )

    def test_full_handoff_scenario_no_duplication(self):
        """Full end-to-end test of the handoff scenario from Issue #2171.

        Simulates: User -> Agent does tool call -> Agent hands off to next agent
        Verifies: Next agent receives summary only, no duplicate raw items.
        """
        agent = _create_mock_agent()

        # Full scenario: tool call in pre_handoff, handoff in new_items
        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "What's the weather?"},),
            pre_handoff_items=(
                _create_tool_call_item(agent),  # function_call
                _create_tool_output_item(agent),  # function_call_output
            ),
            new_items=(
                _create_message_item(agent),  # assistant message
                _create_handoff_call_item(agent),  # function_call (handoff)
                _create_handoff_output_item(agent),  # function_call_output (handoff)
            ),
        )

        nested = nest_handoff_history(handoff_data)

        # Count what would be sent to the model
        total_model_items = (
            len(nested.input_history)  # Summary
            + len(nested.pre_handoff_items)  # Filtered pre-handoff
            + len(nested.input_items or [])  # Filtered new items
        )

        # Before fix: would have 6+ items (summary + raw tool items)
        # After fix: should have ~2 items (summary + message)
        assert total_model_items <= 3, (
            f"Model should receive at most 3 items (summary + messages), got {total_model_items}"
        )

        # Verify no raw function_call_output items in model input
        all_input_items = list(nested.pre_handoff_items) + list(nested.input_items or [])
        function_call_outputs = [
            item
            for item in all_input_items
            if isinstance(item, ToolCallOutputItem | HandoffOutputItem)
        ]
        assert len(function_call_outputs) == 0, (
            "No function_call_output items should be in model input"
        )


@pytest.mark.asyncio
async def test_to_input_list_normalized_uses_filtered_continuation_after_nested_handoff() -> None:
    triage_model = FakeModel()
    delegate_model = FakeModel()

    delegate = Agent(name="delegate", model=delegate_model)
    triage = Agent(name="triage", model=triage_model, handoffs=[delegate])

    triage_model.add_multiple_turn_outputs(
        [[get_text_message("triage summary"), get_handoff_tool_call(delegate)]]
    )
    delegate_model.add_multiple_turn_outputs(
        [
            [get_text_message("resolution")],
            [get_text_message("followup answer")],
        ]
    )

    result = await Runner.run(
        triage,
        input="user_question",
        run_config=RunConfig(nest_handoff_history=True),
    )

    preserve_all_input = result.to_input_list()
    normalized_input = result.to_input_list(mode="normalized")
    preserve_all_types = [
        item.get("type", "message") for item in preserve_all_input if isinstance(item, dict)
    ]
    normalized_types = [
        item.get("type", "message") for item in normalized_input if isinstance(item, dict)
    ]

    assert len(preserve_all_input) == 6
    assert "function_call" in preserve_all_types
    assert "function_call_output" in preserve_all_types
    assert sum(_input_item_text(item) == "triage summary" for item in preserve_all_input) == 1
    assert len(normalized_input) == 4
    assert "function_call" not in normalized_types
    assert "function_call_output" not in normalized_types

    replay_model = FakeModel()
    replay_agent = Agent(name="replay", model=replay_model)
    replay_model.add_multiple_turn_outputs([[get_text_message("replayed")]])
    replay_result = await Runner.run(replay_agent, input=preserve_all_input)

    assert replay_model.first_turn_args is not None
    replay_input = replay_model.first_turn_args["input"]
    assert isinstance(replay_input, list)
    assert sum(_input_item_text(item) == "triage summary" for item in replay_input) == 1
    assert replay_result.final_output == "replayed"

    follow_up_input = normalized_input + [{"role": "user", "content": "follow up?"}]
    follow_up_result = await Runner.run(delegate, input=follow_up_input)

    assert follow_up_result.final_output == "followup answer"
    assert delegate_model.last_turn_args["input"] == follow_up_input


@pytest.mark.asyncio
async def test_to_input_list_normalized_keeps_delegate_tool_items_after_nested_handoff() -> None:
    async def lookup_weather(city: str) -> str:
        return f"weather:{city}"

    triage_model = FakeModel()
    delegate_model = FakeModel()

    delegate = Agent(
        name="delegate",
        model=delegate_model,
        tools=[function_tool(lookup_weather, name_override="lookup_weather")],
    )
    triage = Agent(name="triage", model=triage_model, handoffs=[delegate])

    triage_model.add_multiple_turn_outputs(
        [[get_text_message("triage summary"), get_handoff_tool_call(delegate)]]
    )
    delegate_model.add_multiple_turn_outputs(
        [
            [
                get_text_message("delegate preamble"),
                get_function_tool_call("lookup_weather", json.dumps({"city": "Tokyo"})),
            ],
            [get_text_message("resolution")],
        ]
    )

    result = await Runner.run(
        triage,
        input="user_question",
        run_config=RunConfig(nest_handoff_history=True),
    )

    preserve_all_input = result.to_input_list()
    normalized_input = result.to_input_list(mode="normalized")
    preserve_all_function_calls = [
        cast(dict[str, Any], item)
        for item in preserve_all_input
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    preserve_all_function_outputs = [
        cast(dict[str, Any], item)
        for item in preserve_all_input
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    function_calls = [
        cast(dict[str, Any], item)
        for item in normalized_input
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    function_outputs = [
        cast(dict[str, Any], item)
        for item in normalized_input
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]

    assert len(preserve_all_function_calls) == 2
    assert len(preserve_all_function_outputs) == 2
    assert len(function_calls) == 1
    assert function_calls[0]["name"] == "lookup_weather"
    assert len(function_outputs) == 1
    assert function_outputs[0]["output"] == "weather:Tokyo"


@pytest.mark.asyncio
async def test_to_input_list_normalized_uses_custom_filter_input_items() -> None:
    def keep_messages_only(data: HandoffInputData) -> HandoffInputData:
        return data.clone(
            input_items=tuple(
                item for item in data.new_items if isinstance(item, MessageOutputItem)
            )
        )

    triage_model = FakeModel()
    delegate_model = FakeModel()

    delegate = Agent(name="delegate", model=delegate_model)
    triage = Agent(
        name="triage",
        model=triage_model,
        handoffs=[handoff(delegate, input_filter=keep_messages_only)],
    )

    triage_model.add_multiple_turn_outputs(
        [[get_text_message("triage summary"), get_handoff_tool_call(delegate)]]
    )
    delegate_model.add_multiple_turn_outputs([[get_text_message("resolution")]])

    result = await Runner.run(triage, input="user_question")
    preserve_all_input = result.to_input_list()
    normalized_input = result.to_input_list(mode="normalized")
    preserve_all_types = [
        item.get("type", "message") for item in preserve_all_input if isinstance(item, dict)
    ]
    normalized_types = [
        item.get("type", "message") for item in normalized_input if isinstance(item, dict)
    ]

    assert len(preserve_all_input) == 5
    assert "function_call" in preserve_all_types
    assert "function_call_output" in preserve_all_types
    assert len(normalized_input) == 3
    assert "function_call" not in normalized_types
    assert "function_call_output" not in normalized_types


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_non_nested_filtered_handoff_does_not_add_occurrence_lineage(
    streamed: bool,
) -> None:
    """Ordinary filtered handoffs must not mutate RunItems with nested-history metadata."""

    def identity_filter(data: HandoffInputData) -> HandoffInputData:
        return data

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=identity_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    if streamed:
        streamed_result = Runner.run_streamed(first_agent, input="start")
        async for _ in streamed_result.stream_events():
            pass
        new_items = streamed_result.new_items
    else:
        result = await Runner.run(first_agent, input="start")
        new_items = result.new_items

    assert all(nested_history_run_item_occurrence_key(run_item) is None for run_item in new_items)


@pytest.mark.asyncio
async def test_custom_filter_summary_shape_does_not_claim_equal_session_item() -> None:
    """SDK-shaped custom history must not create implicit ownership by payload equality."""

    def custom_filter(data: HandoffInputData) -> HandoffInputData:
        raw_message = deepcopy(data.new_items[0].to_input_item())
        first_summary = default_handoff_history_mapper(
            [cast(TResponseInputItem, {"role": "user", "content": "custom first"})]
        )[0]
        second_summary = default_handoff_history_mapper(
            [cast(TResponseInputItem, {"role": "user", "content": "custom second"})]
        )[0]
        return data.clone(
            input_history=(first_summary, raw_message, second_summary),
            pre_handoff_items=(),
            input_items=(),
        )

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=custom_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(first_agent, input="start")

    assert sum(_input_item_text(item) == "same" for item in result.to_input_list()) == 2


@pytest.mark.asyncio
async def test_wrapper_reset_does_not_change_nested_history_ownership() -> None:
    """Replay ownership must not depend on wrappers that are current after the run."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("handoff message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    set_conversation_history_wrappers(start="<<START>>", end="<<END>>")
    try:
        result = await Runner.run(
            first_agent,
            input="start",
            run_config=RunConfig(nest_handoff_history=True),
        )
    finally:
        reset_conversation_history_wrappers()

    assert sum(_input_item_text(item) == "handoff message" for item in result.to_input_list()) == 1


@pytest.mark.parametrize("chained", [False, True], ids=["direct", "chained"])
@pytest.mark.asyncio
async def test_public_nested_history_filter_preserves_ownership(chained: bool) -> None:
    """The exported nesting filter should retain provenance through built-in filter chains."""

    def chained_filter(data: HandoffInputData) -> HandoffInputData:
        return remove_all_tools(nest_handoff_history(data))

    input_filter = chained_filter if chained else nest_handoff_history
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=input_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("handoff message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(first_agent, input="start")

    assert sum(_input_item_text(item) == "handoff message" for item in result.to_input_list()) == 1


@pytest.mark.asyncio
async def test_public_nested_history_filter_preserves_ownership_after_deepcopy() -> None:
    """Copying nested input must preserve occurrence provenance for filter composition."""

    def copied_filter(data: HandoffInputData) -> HandoffInputData:
        nested = nest_handoff_history(data)
        assert not isinstance(nested.input_history, str)
        return nested.clone(input_history=deepcopy(nested.input_history))

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=copied_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(first_agent, input="start")

    assert sum(_input_item_text(item) == "same" for item in result.to_input_list()) == 1


@pytest.mark.asyncio
async def test_public_nested_history_filter_preserves_ownership_after_dict_rebuild() -> None:
    """Rebuilding nested input mappings must preserve occurrence provenance."""

    def rebuilt_filter(data: HandoffInputData) -> HandoffInputData:
        nested = nest_handoff_history(data)
        assert not isinstance(nested.input_history, str)
        return nested.clone(
            input_history=tuple(
                cast(TResponseInputItem, dict(item)) if isinstance(item, dict) else item
                for item in nested.input_history
            )
        )

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=rebuilt_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(first_agent, input="start")

    assert sum(_input_item_text(item) == "same" for item in result.to_input_list()) == 1


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_public_nested_history_filter_rebases_owned_input_after_insertion(
    streamed: bool,
) -> None:
    """Inserting clean history items must not invalidate forwarded occurrence ownership."""

    def inserting_filter(data: HandoffInputData) -> HandoffInputData:
        nested = nest_handoff_history(data)
        assert not isinstance(nested.input_history, str)
        inserted = cast(TResponseInputItem, {"role": "user", "content": "inserted"})
        rebuilt_history = tuple(
            cast(TResponseInputItem, dict(item)) if isinstance(item, dict) else item
            for item in nested.input_history
        )
        return nested.clone(input_history=(inserted, *rebuilt_history))

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=inserting_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    if streamed:
        streamed_result = Runner.run_streamed(first_agent, input="start")
        async for _ in streamed_result.stream_events():
            pass
        replay_input = streamed_result.to_input_list()
    else:
        result = await Runner.run(first_agent, input="start")
        replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "same" for item in replay_input) == 1


@pytest.mark.asyncio
async def test_public_nested_history_filter_data_rebuild_drops_private_ownership() -> None:
    """A manually rebuilt container must not infer ownership from equal payloads."""

    def rebuilt_filter(data: HandoffInputData) -> HandoffInputData:
        nested = nest_handoff_history(data)
        return HandoffInputData(
            input_history=nested.input_history,
            pre_handoff_items=nested.pre_handoff_items,
            new_items=nested.new_items,
            run_context=nested.run_context,
            input_items=nested.input_items,
        )

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=rebuilt_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(first_agent, input="start")

    assert sum(_input_item_text(item) == "same" for item in result.to_input_list()) == 2


@pytest.mark.asyncio
async def test_public_nested_history_filter_preserves_ownership_after_new_items_copy() -> None:
    """Cloning filtered run items must preserve the private occurrence ledger."""

    def copied_filter(data: HandoffInputData) -> HandoffInputData:
        nested = nest_handoff_history(data)
        return nested.clone(new_items=deepcopy(nested.new_items))

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=copied_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(first_agent, input="start")

    assert sum(_input_item_text(item) == "same" for item in result.to_input_list()) == 1


@pytest.mark.asyncio
async def test_public_nested_history_filter_does_not_own_equal_replacement() -> None:
    """An equal new RunItem must remain a distinct replay occurrence."""

    def replacement_filter(data: HandoffInputData) -> HandoffInputData:
        nested = nest_handoff_history(data)
        original = cast(MessageOutputItem, nested.new_items[0])
        replacement = MessageOutputItem(
            agent=original.agent,
            raw_item=deepcopy(original.raw_item),
        )
        return nested.clone(new_items=(replacement, *nested.new_items[1:]))

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=replacement_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(first_agent, input="start")

    assert sum(_input_item_text(item) == "same" for item in result.to_input_list()) == 2


def test_nested_history_private_ownership_is_not_a_dataclass_field() -> None:
    """The private ownership ledger must stay out of public dataclass serialization."""
    agent = _create_mock_agent()
    nested = nest_handoff_history(
        HandoffInputData(
            input_history="start",
            pre_handoff_items=(),
            new_items=(_create_message_item(agent, text="same"),),
        )
    )

    assert "_nested_history_owned_items" not in {
        item_field.name for item_field in dataclasses.fields(HandoffInputData)
    }
    serialized = dataclasses.asdict(nested.clone(new_items=(), input_items=()))
    assert "_nested_history_owned_items" not in serialized


def test_nested_history_input_rebase_rejects_ambiguous_equal_occurrences() -> None:
    """Copied input payloads must not claim ownership when an equal occurrence is ambiguous."""
    agent = _create_mock_agent()
    nested = nest_handoff_history(
        HandoffInputData(
            input_history="start",
            pre_handoff_items=(),
            new_items=(_create_message_item(agent, text="same"),),
        )
    )
    assert not isinstance(nested.input_history, str)
    forwarded = next(
        item
        for item in nested.input_history
        if item.get("type") == "message" and item.get("role") == "assistant"
    )
    rebuilt = nested.clone(
        input_history=(
            *deepcopy(nested.input_history),
            deepcopy(forwarded),
        )
    )

    assert _get_nested_history_owned_items(rebuilt) == ()


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_nested_history_preserves_repeated_run_item_reference_occurrences(
    streamed: bool,
) -> None:
    """Repeating one RunItem object must preserve both logical occurrences."""

    def duplicate_message_filter(data: HandoffInputData) -> HandoffInputData:
        message = data.new_items[0]
        return nest_handoff_history(data.clone(new_items=(message, message, *data.new_items[1:])))

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=duplicate_message_filter)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    if streamed:
        streamed_result = Runner.run_streamed(first_agent, input="start")
        async for _ in streamed_result.stream_events():
            pass
        replay_input = streamed_result.to_input_list()
    else:
        result = await Runner.run(first_agent, input="start")
        replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "same" for item in replay_input) == 2


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_nested_history_retains_forwarded_pre_handoff_item_provenance(
    streamed: bool,
) -> None:
    """Lossless items from earlier turns must retain one replay occurrence."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    tool_search_call = ResponseToolSearchCall(
        id="tool_search_call",
        call_id="search",
        arguments={"query": "profile"},
        execution="server",
        status="completed",
        type="tool_search_call",
    )
    tool_search_output = ResponseToolSearchOutputItem(
        id="tool_search_output",
        call_id="search",
        execution="server",
        status="completed",
        tools=[],
        type="tool_search_output",
    )
    first_model.add_multiple_turn_outputs(
        [
            [tool_search_call, tool_search_output],
            [get_handoff_tool_call(second_agent)],
        ]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])
    run_config = RunConfig(nest_handoff_history=True)
    run_result: RunResult | RunResultStreaming

    if streamed:
        run_result = Runner.run_streamed(first_agent, input="start", run_config=run_config)
        async for _ in run_result.stream_events():
            pass
    else:
        run_result = await Runner.run(first_agent, input="start", run_config=run_config)

    replay_input = run_result.to_input_list()
    replay_types = [item.get("type") for item in replay_input]
    assert replay_types.count("tool_search_call") == 1
    assert replay_types.count("tool_search_output") == 1
    assert isinstance(run_result.input, list)
    assert all("_agents_nested_history_token" not in item for item in run_result.input)

    owned_types = {
        item_ref.run_item.type
        for item_ref in run_result._nested_history_owned_session_item_refs
        if item_ref.run_item is not None
    }
    assert {"tool_search_call_item", "tool_search_output_item"} <= owned_types

    state_json = run_result.to_state().to_json()
    assert all("_agents_nested_history_token" not in item for item in state_json["original_input"])
    restored = await RunState.from_json(first_agent, state_json)
    restored_owned_types = {
        item_ref.run_item.type
        for item_ref in restored._nested_history_owned_session_item_refs
        if item_ref.run_item is not None
    }
    assert {"tool_search_call_item", "tool_search_output_item"} <= restored_owned_types


@pytest.mark.asyncio
async def test_to_input_list_during_active_stream_does_not_mutate_input() -> None:
    """Inspecting an active stream must not mutate its eventual public input."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("handoff message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = Runner.run_streamed(
        first_agent,
        input="start",
        run_config=RunConfig(nest_handoff_history=True),
    )
    input_before_inspection = deepcopy(result.input)

    assert result.to_input_list()
    assert result.input == input_before_inspection

    async for _ in result.stream_events():
        pass

    assert isinstance(result.input, list)
    assert all("_agents_nested_history_token" not in item for item in result.input)
    assert sum(_input_item_text(item) == "handoff message" for item in result.to_input_list()) == 1


@pytest.mark.asyncio
async def test_nested_history_ownership_remaps_after_new_items_insertion() -> None:
    """A caller inserting a public new_items entry must not make ownership drop the new item."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("handoff message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(
        first_agent,
        input="start",
        run_config=RunConfig(nest_handoff_history=True),
    )
    result.new_items.insert(0, _create_message_item(first_agent, text="inserted"))
    replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "inserted" for item in replay_input) == 1
    assert sum(_input_item_text(item) == "handoff message" for item in replay_input) == 1


@pytest.mark.asyncio
async def test_nested_history_input_removal_does_not_claim_an_unmarked_equal_occurrence() -> None:
    """An equal replacement input must not retain ownership of the removed occurrence."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(
        first_agent,
        input="start",
        run_config=RunConfig(nest_handoff_history=True),
    )
    assert isinstance(result.input, list)
    owned_index = result._nested_history_owned_session_item_refs[0].input_index
    owned_input = result.input.pop(owned_index)
    result.input.append(deepcopy(owned_input))

    replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "same" for item in replay_input) == 2


@pytest.mark.asyncio
async def test_nested_history_new_item_removal_does_not_claim_an_equal_item() -> None:
    """Removing the owned RunItem must not transfer ownership to an equal RunItem."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(
        first_agent,
        input="start",
        run_config=RunConfig(nest_handoff_history=True),
    )
    owned_run_item = next(
        item_ref.run_item
        for item_ref in result._nested_history_owned_session_item_refs
        if item_ref.run_item is not None
    )
    equal_unowned_item = _create_message_item(first_agent, text="same")
    result.new_items[:] = [equal_unowned_item] + [
        item for item in result.new_items if item is not owned_run_item
    ]

    replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "same" for item in replay_input) == 2
    state = result.to_state()
    assert state._nested_history_owned_session_item_refs == []
    state.to_json()


@pytest.mark.asyncio
async def test_nested_history_ownership_survives_result_new_items_copy() -> None:
    """Copying result run items must not replay a nested session occurrence twice."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(
        first_agent,
        input="start",
        run_config=RunConfig(nest_handoff_history=True),
    )
    result.new_items = deepcopy(result.new_items)

    replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "same" for item in replay_input) == 1


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_nested_history_input_copy_does_not_infer_occurrence_ownership(
    streamed: bool,
) -> None:
    """An unmarked public-input copy must remain distinct from its session occurrence."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result: RunResult | RunResultStreaming
    if streamed:
        result = Runner.run_streamed(
            first_agent,
            input="start",
            run_config=RunConfig(nest_handoff_history=True),
        )
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(
            first_agent,
            input="start",
            run_config=RunConfig(nest_handoff_history=True),
        )

    assert isinstance(result.input, list)
    result.input = deepcopy(result.input)

    replay_input = result.to_input_list()
    state = result.to_state()

    assert sum(_input_item_text(item) == "same" for item in replay_input) == 2
    assert state._nested_history_owned_session_item_refs
    state.to_json()


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_nested_history_input_copy_and_reorder_does_not_infer_ownership(
    streamed: bool,
) -> None:
    """Payload equality must not transfer ownership after a copied input reorder."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("owned once"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])
    run_config = RunConfig(nest_handoff_history=True)

    result: RunResult | RunResultStreaming
    if streamed:
        result = Runner.run_streamed(first_agent, input="start", run_config=run_config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(first_agent, input="start", run_config=run_config)

    assert isinstance(result.input, list)
    result.input = list(reversed(deepcopy(result.input)))

    replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "owned once" for item in replay_input) == 2


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.parametrize("mutation", ["remove", "reorder"])
@pytest.mark.asyncio
async def test_result_input_mutation_does_not_change_state_snapshot_ownership(
    streamed: bool,
    mutation: str,
) -> None:
    """RunState ownership must follow its saved snapshot, not the mutable result view."""

    @function_tool(needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model, tools=[approval_tool])
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("owned once"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="approval")],
            [get_text_message("done")],
        ]
    )
    run_config = RunConfig(nest_handoff_history=True)

    interrupted: RunResult | RunResultStreaming
    if streamed:
        interrupted = Runner.run_streamed(first_agent, input="start", run_config=run_config)
        async for _ in interrupted.stream_events():
            pass
    else:
        interrupted = await Runner.run(first_agent, input="start", run_config=run_config)

    assert len(interrupted.interruptions) == 1
    assert isinstance(interrupted.input, list)
    owned_index = interrupted._nested_history_owned_session_item_refs[0].input_index
    owned_item = interrupted.input[owned_index]
    if mutation == "remove":
        interrupted.input.pop(owned_index)
    else:
        target_index = 0 if owned_index != 0 else len(interrupted.input) - 1
        interrupted.input.pop(owned_index)
        interrupted.input.insert(target_index, owned_item)
        moved_index = next(
            index for index, item in enumerate(interrupted.input) if item is owned_item
        )
        assert moved_index != owned_index

    state = interrupted.to_state()
    assert state._nested_history_owned_session_item_refs
    state.approve(interrupted.interruptions[0])
    restored = await RunState.from_string(first_agent, state.to_string())

    resumed: RunResult | RunResultStreaming
    if streamed:
        resumed = Runner.run_streamed(first_agent, restored, run_config=run_config)
        async for _ in resumed.stream_events():
            pass
    else:
        resumed = await Runner.run(first_agent, restored, run_config=run_config)

    assert resumed.final_output == "done"
    assert sum(_input_item_text(item) == "owned once" for item in resumed.to_input_list()) == 1


@pytest.mark.asyncio
async def test_nested_history_ownership_revalidates_after_input_removal() -> None:
    """Removing an owned input occurrence must restore its session copy during replay."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("handoff message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    result = await Runner.run(
        first_agent,
        input="start",
        run_config=RunConfig(nest_handoff_history=True),
    )
    assert isinstance(result.input, list)
    result.input[:] = [item for item in result.input if _input_item_text(item) != "handoff message"]

    replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "handoff message" for item in replay_input) == 1


def test_nested_history_session_resolution_requires_exact_run_item_identity() -> None:
    """Session ownership must not bind to an older equal RunItem."""
    agent = _create_mock_agent()
    older = _create_message_item(agent, text="same")
    owned = _create_message_item(agent, text="same")
    owned_input = owned.to_input_item()
    digest = digest_input_item(owned_input)
    assert digest is not None

    refs = resolve_nested_history_owned_session_item_refs(
        [older],
        [owned_input],
        [NestedHistoryOwnedItem(run_item=owned, input_index=0, digest=digest)],
    )

    assert refs == []


def test_nested_history_session_resolution_keeps_surviving_owned_items() -> None:
    """One missing owned RunItem must not discard another exact ownership match."""
    agent = _create_mock_agent()
    kept = _create_message_item(agent, text="kept")
    removed = _create_message_item(agent, text="removed")
    kept_input = kept.to_input_item()
    removed_input = removed.to_input_item()
    kept_digest = digest_input_item(kept_input)
    removed_digest = digest_input_item(removed_input)
    assert kept_digest is not None
    assert removed_digest is not None

    refs = resolve_nested_history_owned_session_item_refs(
        [kept],
        [kept_input, removed_input],
        [
            NestedHistoryOwnedItem(run_item=kept, input_index=0, digest=kept_digest),
            NestedHistoryOwnedItem(run_item=removed, input_index=1, digest=removed_digest),
        ],
    )

    assert len(refs) == 1
    assert refs[0].run_item is kept


def test_nested_history_session_resolution_uses_copy_lineage_per_occurrence() -> None:
    """Separately copied repeated references must resolve to separate session occurrences."""
    agent = _create_mock_agent()
    repeated = _create_message_item(agent, text="same")
    nested = nest_handoff_history(
        HandoffInputData(
            input_history="start",
            pre_handoff_items=(),
            new_items=(repeated, repeated),
        )
    )
    assert not isinstance(nested.input_history, str)
    copied_session_items = [deepcopy(repeated), deepcopy(repeated)]

    refs = resolve_nested_history_owned_session_item_refs(
        copied_session_items,
        nested.input_history,
        _get_nested_history_owned_items(nested),
    )

    assert len(refs) == 2
    assert refs[0].run_item is copied_session_items[0]
    assert refs[1].run_item is copied_session_items[1]


def test_nested_history_normalizes_forwarded_status_before_ownership() -> None:
    """Forwarded payloads and their session digests must use canonical replay normalization."""
    agent = _create_mock_agent()
    raw_message = ResponseOutputMessage.model_construct(
        id="msg_none_status",
        content=[ResponseOutputText(text="same", type="output_text", annotations=[])],
        role="assistant",
        status=None,
        type="message",
    )
    run_item = MessageOutputItem(agent=agent, raw_item=raw_message)
    nested = nest_handoff_history(
        HandoffInputData(
            input_history="start",
            pre_handoff_items=(),
            new_items=(run_item,),
        )
    )
    assert not isinstance(nested.input_history, str)
    forwarded = next(
        item
        for item in nested.input_history
        if item.get("type") == "message" and item.get("role") == "assistant"
    )

    assert "status" not in forwarded
    refs = resolve_nested_history_owned_session_item_refs(
        [run_item],
        nested.input_history,
        _get_nested_history_owned_items(nested),
    )
    assert len(refs) == 1


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_plain_handoff_preserves_prior_nested_history_ownership(streamed: bool) -> None:
    """A later non-nesting handoff must not clear ownership established by an earlier handoff."""
    first_model = FakeModel()
    second_model = FakeModel()
    final_model = FakeModel()
    final_agent = Agent(name="final", model=final_model)
    second_agent = Agent(
        name="second",
        model=second_model,
        handoffs=[handoff(final_agent, nest_handoff_history=False)],
    )
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("first message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs(
        [[get_text_message("second message"), get_handoff_tool_call(final_agent)]]
    )
    final_model.add_multiple_turn_outputs([[get_text_message("done")]])
    run_config = RunConfig(nest_handoff_history=True)

    if streamed:
        streamed_result = Runner.run_streamed(first_agent, input="start", run_config=run_config)
        async for _ in streamed_result.stream_events():
            pass
        replay_input = streamed_result.to_input_list()
    else:
        result = await Runner.run(first_agent, input="start", run_config=run_config)
        replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "first message" for item in replay_input) == 1


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_non_nested_copying_filter_preserves_prior_nested_history_ownership(
    streamed: bool,
) -> None:
    """A later non-nesting filter may copy history without losing prior ownership."""

    def copy_history(data: HandoffInputData) -> HandoffInputData:
        if isinstance(data.input_history, str):
            return data
        return data.clone(input_history=deepcopy(data.input_history))

    first_model = FakeModel()
    second_model = FakeModel()
    final_model = FakeModel()
    final_agent = Agent(name="final", model=final_model)
    second_agent = Agent(
        name="second",
        model=second_model,
        handoffs=[
            handoff(
                final_agent,
                input_filter=copy_history,
                nest_handoff_history=False,
            )
        ],
    )
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("first message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs(
        [[get_text_message("second message"), get_handoff_tool_call(final_agent)]]
    )
    final_model.add_multiple_turn_outputs([[get_text_message("done")]])
    run_config = RunConfig(nest_handoff_history=True)

    if streamed:
        streamed_result = Runner.run_streamed(
            first_agent,
            input="start",
            run_config=run_config,
        )
        async for _ in streamed_result.stream_events():
            pass
        replay_input = streamed_result.to_input_list()
    else:
        result = await Runner.run(first_agent, input="start", run_config=run_config)
        replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "first message" for item in replay_input) == 1


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_copied_custom_input_items_keep_session_occurrence_for_later_nesting(
    streamed: bool,
) -> None:
    """Copied model items must still resolve to their exact session occurrences."""

    def copy_model_items(data: HandoffInputData) -> HandoffInputData:
        return data.clone(input_items=deepcopy(data.new_items))

    first_model = FakeModel()
    second_model = FakeModel()
    final_model = FakeModel()
    final_agent = Agent(name="final", model=final_model)
    second_agent = Agent(name="second", model=second_model, handoffs=[final_agent])
    first_agent = Agent(
        name="first",
        model=first_model,
        handoffs=[handoff(second_agent, input_filter=copy_model_items)],
    )
    first_model.add_multiple_turn_outputs(
        [[get_text_message("copied once"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_handoff_tool_call(final_agent)]])
    final_model.add_multiple_turn_outputs([[get_text_message("done")]])
    run_config = RunConfig(nest_handoff_history=True)

    if streamed:
        streamed_result = Runner.run_streamed(first_agent, input="start", run_config=run_config)
        async for _ in streamed_result.stream_events():
            pass
        replay_input = streamed_result.to_input_list()
    else:
        result = await Runner.run(first_agent, input="start", run_config=run_config)
        replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "copied once" for item in replay_input) == 1


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_identity_filter_preserves_prior_nested_history_ownership(streamed: bool) -> None:
    """A later no-op filter must retain ownership represented in the filtered input."""

    def identity_filter(data: HandoffInputData) -> HandoffInputData:
        return data

    first_model = FakeModel()
    second_model = FakeModel()
    final_model = FakeModel()
    final_agent = Agent(name="final", model=final_model)
    second_agent = Agent(
        name="second",
        model=second_model,
        handoffs=[handoff(final_agent, input_filter=identity_filter)],
    )
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    first_model.add_multiple_turn_outputs(
        [[get_text_message("first message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs(
        [[get_text_message("second message"), get_handoff_tool_call(final_agent)]]
    )
    final_model.add_multiple_turn_outputs([[get_text_message("done")]])
    run_config = RunConfig(nest_handoff_history=True)

    if streamed:
        streamed_result = Runner.run_streamed(first_agent, input="start", run_config=run_config)
        async for _ in streamed_result.stream_events():
            pass
        replay_input = streamed_result.to_input_list()
    else:
        result = await Runner.run(first_agent, input="start", run_config=run_config)
        replay_input = result.to_input_list()

    assert sum(_input_item_text(item) == "first message" for item in replay_input) == 1


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_nested_handoff_history_preserves_identical_messages_across_turns(
    streamed: bool,
) -> None:
    """Raw-tail ownership should preserve equal messages from distinct logical turns."""

    @function_tool
    def continue_work() -> str:
        return "continue"

    first_model = FakeModel()
    second_model = FakeModel()
    final_model = FakeModel()
    final_agent = Agent(name="final", model=final_model)
    second_agent = Agent(
        name="second",
        model=second_model,
        tools=[continue_work],
        handoffs=[final_agent],
    )
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])

    first_model.add_multiple_turn_outputs(
        [[get_text_message("same"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs(
        [
            [get_text_message("same"), get_function_tool_call("continue_work", "{}")],
            [get_text_message("same"), get_handoff_tool_call(final_agent)],
        ]
    )
    final_model.add_multiple_turn_outputs([[get_text_message("same")]])

    if streamed:
        streamed_result = Runner.run_streamed(
            first_agent,
            input="start",
            run_config=RunConfig(nest_handoff_history=True),
        )
        async for _ in streamed_result.stream_events():
            pass
        replay_input = streamed_result.to_input_list()
    else:
        result = await Runner.run(
            first_agent,
            input="start",
            run_config=RunConfig(nest_handoff_history=True),
        )
        replay_input = result.to_input_list()

    final_input = final_model.last_turn_args["input"]
    summary = str(cast(dict[str, Any], final_input[0])["content"])
    assert summary.count("same") == 2
    assert sum(_input_item_text(item) == "same" for item in replay_input) == 4


@pytest.mark.asyncio
async def test_explicit_default_handoff_history_mapper_is_honored() -> None:
    """An explicitly configured mapper should own the exact model input."""
    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])

    first_model.add_multiple_turn_outputs(
        [[get_text_message("handoff message"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])

    await Runner.run(
        first_agent,
        input="start",
        run_config=RunConfig(
            nest_handoff_history=True,
            handoff_history_mapper=default_handoff_history_mapper,
        ),
    )

    assert second_model.first_turn_args is not None
    second_input = second_model.first_turn_args["input"]
    assert isinstance(second_input, list)
    assert len(second_input) == 1
    summary = str(cast(dict[str, Any], second_input[0])["content"])
    assert "handoff message" in summary
    assert "transfer_to_second" in summary


@pytest.mark.asyncio
async def test_nested_handoff_history_partition_survives_interruption_resume() -> None:
    """The summary/raw partition should survive RunState serialization."""

    @function_tool(needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    first_model = FakeModel()
    second_model = FakeModel()
    final_model = FakeModel()
    final_agent = Agent(name="final", model=final_model)
    second_agent = Agent(
        name="second",
        model=second_model,
        tools=[approval_tool],
        handoffs=[final_agent],
    )
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    run_config = RunConfig(nest_handoff_history=True)

    first_model.add_multiple_turn_outputs(
        [[get_text_message("once"), get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="approval")],
            [get_text_message("once"), get_handoff_tool_call(final_agent)],
        ]
    )
    final_model.add_multiple_turn_outputs([[get_text_message("done")]])

    interrupted = await Runner.run(first_agent, input="start", run_config=run_config)
    assert len(interrupted.interruptions) == 1
    assert sum(_input_item_text(item) == "once" for item in interrupted.to_input_list()) == 1
    state = interrupted.to_state()
    assert state._nested_history_owned_session_item_refs
    serialized_refs = state.to_json()["nested_history_owned_session_item_refs"]
    assert all(
        isinstance(item_ref, dict)
        and isinstance(item_ref.get("digest"), str)
        and len(item_ref["digest"]) == 64
        for item_ref in serialized_refs
    )
    state.approve(interrupted.interruptions[0])
    serialized_state = state.to_string()
    restored = await RunState.from_string(first_agent, serialized_state)
    assert restored._nested_history_owned_session_item_refs == (
        state._nested_history_owned_session_item_refs
    )

    resumed = await Runner.run(first_agent, restored, run_config=run_config)

    assert resumed.final_output == "done"
    final_input = final_model.last_turn_args["input"]
    summary = str(cast(dict[str, Any], final_input[0])["content"])
    assert summary.count("once") == 1
    assert sum(_input_item_text(item) == "once" for item in resumed.to_input_list()) == 2


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.parametrize(
    "nest_handoff_history",
    [False, True],
    ids=["flat_history", "nested_history"],
)
@pytest.mark.asyncio
async def test_pending_handoff_in_interrupted_turn_survives_run_state(
    streamed: bool,
    nest_handoff_history: bool,
) -> None:
    """A handoff paused beside an approval must execute after RunState restoration."""

    @function_tool(needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    first_model = FakeModel()
    second_model = FakeModel()
    final_model = FakeModel()
    final_agent = Agent(name="final", model=final_model)
    second_agent = Agent(
        name="second",
        model=second_model,
        tools=[approval_tool],
        handoffs=[final_agent],
    )
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    run_config = RunConfig(nest_handoff_history=nest_handoff_history)

    first_handoff = cast(ResponseFunctionToolCall, get_handoff_tool_call(second_agent))
    first_handoff.call_id = "first-handoff"
    final_handoff = cast(ResponseFunctionToolCall, get_handoff_tool_call(final_agent))
    final_handoff.call_id = "final-handoff"
    first_model.add_multiple_turn_outputs([[get_text_message("first once"), first_handoff]])
    second_model.add_multiple_turn_outputs(
        [
            [
                get_text_message("second once"),
                get_function_tool_call("approval_tool", "{}", call_id="approval"),
                final_handoff,
            ]
        ]
    )
    final_model.add_multiple_turn_outputs([[get_text_message("done")]])

    interrupted: RunResult | RunResultStreaming
    if streamed:
        interrupted = Runner.run_streamed(
            first_agent,
            input="start",
            run_config=run_config,
        )
        async for _ in interrupted.stream_events():
            pass
    else:
        interrupted = await Runner.run(
            first_agent,
            input="start",
            run_config=run_config,
        )

    assert len(interrupted.interruptions) == 1
    state = interrupted.to_state()
    state.approve(interrupted.interruptions[0])
    restored = await RunState.from_string(first_agent, state.to_string())
    assert restored._last_processed_response is not None
    assert len(restored._last_processed_response.handoffs) == 1

    resumed: RunResult | RunResultStreaming
    if streamed:
        resumed = Runner.run_streamed(first_agent, restored, run_config=run_config)
        async for _ in resumed.stream_events():
            pass
    else:
        resumed = await Runner.run(first_agent, restored, run_config=run_config)

    assert resumed.final_output == "done"
    assert resumed.last_agent is final_agent
    assert (
        sum(
            isinstance(item, HandoffOutputItem)
            and cast(dict[str, Any], item.raw_item).get("call_id") == "final-handoff"
            for item in resumed.new_items
        )
        == 1
    )
    if nest_handoff_history:
        final_input = final_model.last_turn_args["input"]
        summary = str(cast(dict[str, Any], final_input[0])["content"])
        assert summary.count("first once") == 1
        assert summary.count("second once") == 1


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.parametrize(
    "nest_handoff_history",
    [False, True],
    ids=["flat_history", "nested_history"],
)
@pytest.mark.parametrize("second_decision", ["approve", "reject"])
@pytest.mark.asyncio
async def test_resumed_handoff_persists_all_staged_approval_outputs(
    streamed: bool,
    nest_handoff_history: bool,
    second_decision: str,
) -> None:
    """Each staged approval result must reach the session before the resumed handoff."""

    tool_calls: list[str] = []
    handoff_count = 0

    @function_tool(needs_approval=True)
    def first_tool() -> str:
        tool_calls.append("first")
        return "first ok"

    @function_tool(needs_approval=True)
    def second_tool() -> str:
        tool_calls.append("second")
        return "second ok"

    class RecordingHooks(RunHooks[Any]):
        async def on_handoff(
            self,
            context: RunContextWrapper[Any],
            from_agent: Agent[Any],
            to_agent: Agent[Any],
        ) -> None:
            nonlocal handoff_count
            handoff_count += 1

    source_model = FakeModel()
    target_model = FakeModel()
    target_agent = Agent(name="target", model=target_model)
    source_agent = Agent(
        name="source",
        model=source_model,
        tools=[first_tool, second_tool],
        handoffs=[target_agent],
    )
    first_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call("first_tool", "{}", call_id="first"),
    )
    second_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call("second_tool", "{}", call_id="second"),
    )
    handoff_call = cast(ResponseFunctionToolCall, get_handoff_tool_call(target_agent))
    first_call.id = "item-first"
    second_call.id = "item-second"
    handoff_call.id = "item-handoff"
    handoff_call.call_id = "handoff"
    source_model.add_multiple_turn_outputs([[first_call, second_call, handoff_call]])
    target_model.add_multiple_turn_outputs([[get_text_message("done")]])

    run_config = RunConfig(nest_handoff_history=nest_handoff_history)
    hooks = RecordingHooks()
    session = SimpleListSession()

    first_result: RunResult | RunResultStreaming
    if streamed:
        first_stream = Runner.run_streamed(
            source_agent,
            "start",
            run_config=run_config,
            hooks=hooks,
            session=session,
        )
        async for _ in first_stream.stream_events():
            pass
        first_result = first_stream
    else:
        first_result = await Runner.run(
            source_agent,
            "start",
            run_config=run_config,
            hooks=hooks,
            session=session,
        )
    assert len(first_result.interruptions) == 2

    state = await RunState.from_string(source_agent, first_result.to_state().to_string())
    first_approval = next(
        item
        for item in state.get_interruptions()
        if cast(ResponseFunctionToolCall, item.raw_item).call_id == "first"
    )
    state.approve(first_approval)
    staged_result: RunResult | RunResultStreaming
    if streamed:
        staged_stream = Runner.run_streamed(
            source_agent,
            state,
            run_config=run_config,
            hooks=hooks,
            session=session,
        )
        async for _ in staged_stream.stream_events():
            pass
        staged_result = staged_stream
    else:
        staged_result = await Runner.run(
            source_agent,
            state,
            run_config=run_config,
            hooks=hooks,
            session=session,
        )
    assert len(staged_result.interruptions) == 1
    assert handoff_count == 0

    state = await RunState.from_string(source_agent, staged_result.to_state().to_string())
    second_approval = state.get_interruptions()[0]
    assert cast(ResponseFunctionToolCall, second_approval.raw_item).call_id == "second"
    getattr(state, second_decision)(second_approval)
    final_result: RunResult | RunResultStreaming
    if streamed:
        final_stream = Runner.run_streamed(
            source_agent,
            state,
            run_config=run_config,
            hooks=hooks,
            session=session,
        )
        async for _ in final_stream.stream_events():
            pass
        final_result = final_stream
    else:
        final_result = await Runner.run(
            source_agent,
            state,
            run_config=run_config,
            hooks=hooks,
            session=session,
        )

    assert final_result.final_output == "done"
    assert final_result.last_agent is target_agent
    assert handoff_count == 1
    assert tool_calls == (["first", "second"] if second_decision == "approve" else ["first"])

    session_items = await session.get_items()
    for call_id in ("first", "second", "handoff"):
        assert (
            sum(
                isinstance(item, dict)
                and item.get("type") == "function_call"
                and item.get("call_id") == call_id
                for item in session_items
            )
            == 1
        )
        assert (
            sum(
                isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item.get("call_id") == call_id
                for item in session_items
            )
            == 1
        )


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.asyncio
async def test_nested_history_resume_to_final_preserves_status_less_ownership(
    streamed: bool,
) -> None:
    """Resume without another handoff must retain one status-less nested occurrence."""

    @function_tool(needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model, tools=[approval_tool])
    first_agent = Agent(name="first", model=first_model, handoffs=[second_agent])
    run_config = RunConfig(nest_handoff_history=True)
    status_less_message = ResponseOutputMessage.model_construct(
        id="msg_none_status",
        content=[ResponseOutputText(text="once", type="output_text", annotations=[])],
        role="assistant",
        status=None,
        type="message",
    )
    first_model.add_multiple_turn_outputs(
        [[status_less_message, get_handoff_tool_call(second_agent)]]
    )
    second_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", "{}", call_id="approval")],
            [get_text_message("done")],
        ]
    )

    if streamed:
        interrupted_stream = Runner.run_streamed(
            first_agent,
            input="start",
            run_config=run_config,
        )
        async for _ in interrupted_stream.stream_events():
            pass
        interruptions = interrupted_stream.interruptions
        state = interrupted_stream.to_state()
    else:
        interrupted_result = await Runner.run(
            first_agent,
            input="start",
            run_config=run_config,
        )
        interruptions = interrupted_result.interruptions
        state = interrupted_result.to_state()

    assert len(interruptions) == 1
    state.approve(interruptions[0])
    restored = await RunState.from_string(first_agent, state.to_string())

    if streamed:
        resumed_stream = Runner.run_streamed(first_agent, restored, run_config=run_config)
        async for _ in resumed_stream.stream_events():
            pass
        final_output = resumed_stream.final_output
        replay_input = resumed_stream.to_input_list()
    else:
        resumed_result = await Runner.run(first_agent, restored, run_config=run_config)
        final_output = resumed_result.final_output
        replay_input = resumed_result.to_input_list()

    assert final_output == "done"
    assert sum(_input_item_text(item) == "once" for item in replay_input) == 1
    assert all("_agents_nested_history_token" not in item for item in replay_input)
    assert second_model.last_turn_args is not None
    assert all(
        "_agents_nested_history_token" not in item for item in second_model.last_turn_args["input"]
    )


@pytest.mark.parametrize("streamed", [False, True], ids=["non_streamed", "streamed"])
@pytest.mark.parametrize(
    "legacy_snapshot",
    [False, True],
    ids=["current_schema", "schema_1_12"],
)
@pytest.mark.asyncio
async def test_first_nested_handoff_after_restore_uses_explicit_occurrence_lineage(
    streamed: bool,
    legacy_snapshot: bool,
) -> None:
    """Current snapshots restore lineage while legacy snapshots remain conservative."""

    @function_tool(needs_approval=True)
    def approval_tool() -> str:
        return "approved"

    first_model = FakeModel()
    second_model = FakeModel()
    second_agent = Agent(name="second", model=second_model)
    first_agent = Agent(
        name="first",
        model=first_model,
        tools=[approval_tool],
        handoffs=[second_agent],
    )
    tool_search_call = ResponseToolSearchCall(
        id="tool_search_call",
        call_id="search",
        arguments={"query": "profile"},
        execution="server",
        status="completed",
        type="tool_search_call",
    )
    tool_search_output = ResponseToolSearchOutputItem(
        id="tool_search_output",
        call_id="search",
        execution="server",
        status="completed",
        tools=[],
        type="tool_search_output",
    )
    first_model.add_multiple_turn_outputs(
        [
            [
                tool_search_call,
                tool_search_output,
                get_function_tool_call("approval_tool", "{}", call_id="approval"),
            ],
            [get_handoff_tool_call(second_agent)],
        ]
    )
    second_model.add_multiple_turn_outputs([[get_text_message("done")]])
    run_config = RunConfig(nest_handoff_history=True)
    interrupted: RunResult | RunResultStreaming

    if streamed:
        interrupted = Runner.run_streamed(first_agent, input="start", run_config=run_config)
        async for _ in interrupted.stream_events():
            pass
    else:
        interrupted = await Runner.run(first_agent, input="start", run_config=run_config)

    assert len(interrupted.interruptions) == 1
    state = interrupted.to_state()
    state.approve(interrupted.interruptions[0])
    state._session_items.insert(0, _create_message_item(first_agent, text="session only"))
    state_json = state.to_json()
    if legacy_snapshot:
        state_json["$schemaVersion"] = "1.12"
        state_json.pop("nested_history_owned_session_item_refs")
        state_json.pop("generated_session_item_indexes")
    restored = await RunState.from_json(first_agent, state_json)
    resumed: RunResult | RunResultStreaming

    if streamed:
        resumed = Runner.run_streamed(first_agent, restored, run_config=run_config)
        async for _ in resumed.stream_events():
            pass
    else:
        resumed = await Runner.run(first_agent, restored, run_config=run_config)

    replay_types = [item.get("type") for item in resumed.to_input_list()]
    expected_occurrences = 2 if legacy_snapshot else 1
    assert replay_types.count("tool_search_call") == expected_occurrences
    assert replay_types.count("tool_search_output") == expected_occurrences
