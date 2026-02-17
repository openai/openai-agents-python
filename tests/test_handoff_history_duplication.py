"""Tests for handoff history duplication fix (Issue #2171).

These tests verify that when nest_handoff_history is enabled,
function_call and function_call_output items are NOT duplicated
in the input sent to the next agent.
"""

from typing import Any, cast

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary

from agents import Agent
from agents.handoffs import HandoffInputData, nest_handoff_history
from agents.items import (
    HandoffCallItem,
    HandoffOutputItem,
    MessageOutputItem,
    ReasoningItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
)


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


def _create_message_item(agent: Agent) -> MessageOutputItem:
    """Create a mock MessageOutputItem."""
    raw_item = ResponseOutputMessage(
        id="msg_123",
        content=[ResponseOutputText(text="Hello!", type="output_text", annotations=[])],
        role="assistant",
        status="completed",
        type="message",
    )
    return MessageOutputItem(agent=agent, raw_item=raw_item, type="message_output_item")


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

        Message items have a 'role' and should NOT be filtered from input_items.
        Note: pre_handoff_items are converted to summary text regardless of type.
        """
        agent = _create_mock_agent()

        handoff_data = HandoffInputData(
            input_history=({"role": "user", "content": "Hello"},),
            pre_handoff_items=(),  # pre_handoff items go into summary
            new_items=(_create_message_item(agent),),
        )

        nested = nest_handoff_history(handoff_data)

        # Message items should be preserved in new_items
        assert len(nested.new_items) == 1, "MessageOutputItem should be preserved in new_items"
        # And in input_items (since it has a role)
        assert nested.input_items is not None
        assert len(nested.input_items) == 1, "MessageOutputItem should be preserved in input_items"
        assert isinstance(nested.input_items[0], MessageOutputItem)

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
            if isinstance(item, (ToolCallOutputItem, HandoffOutputItem))
        ]
        assert len(function_call_outputs) == 0, (
            "No function_call_output items should be in model input"
        )
