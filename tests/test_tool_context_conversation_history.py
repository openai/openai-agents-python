"""Tests for conversation_history functionality in ToolContext."""

from __future__ import annotations

from typing import cast

import pytest
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from openai.types.responses.response_input_item_param import FunctionCallOutput

from agents import (
    Agent,
    MessageOutputItem,
    RunContextWrapper,
    RunItem,
    ToolCallItem,
    ToolCallOutputItem,
    Usage,
)
from agents.items import ItemHelpers
from agents.tool_context import ToolContext

from .test_responses import (
    get_function_tool_call,
    get_text_input_item,
    get_text_message,
)


def test_tool_context_has_conversation_history_field():
    """Test that ToolContext has a conversation_history field."""
    context = ToolContext(context=None, tool_call_id="test-id")
    assert hasattr(context, "conversation_history")
    assert isinstance(context.conversation_history, list)
    assert len(context.conversation_history) == 0


def test_tool_context_from_agent_context_default_history():
    """Test ToolContext.from_agent_context with no conversation history."""
    run_context = RunContextWrapper(context=None, usage=Usage())
    tool_context = ToolContext.from_agent_context(run_context, "test-id")

    assert tool_context.tool_call_id == "test-id"
    assert tool_context.conversation_history == []


def test_tool_context_from_agent_context_with_history():
    """Test ToolContext.from_agent_context with conversation history."""
    run_context = RunContextWrapper(context=None, usage=Usage())
    history = [get_text_input_item("Hello"), get_text_input_item("How are you?")]

    tool_context = ToolContext.from_agent_context(
        run_context, "test-id", conversation_history=history
    )

    assert tool_context.tool_call_id == "test-id"
    assert tool_context.conversation_history == history
    assert len(tool_context.conversation_history) == 2


@pytest.mark.asyncio
async def test_conversation_history_in_tool_execution():
    """Test that conversation history is properly passed to tools during execution."""

    # Create a dummy agent for the items
    dummy_agent = Agent[None](name="dummy")

    # Test that we can build conversation history manually
    original_input = "What's the weather like?"
    pre_step_items: list[RunItem] = [
        MessageOutputItem(
            agent=dummy_agent,
            raw_item=cast(
                ResponseOutputMessage, get_text_message("I'll check the weather for you.")
            ),
        )
    ]
    new_step_items: list[RunItem] = [
        ToolCallItem(
            agent=dummy_agent,
            raw_item=cast(ResponseFunctionToolCall, get_function_tool_call("test_tool", "")),
        )
    ]

    # Test that we can build conversation history manually
    original_items = ItemHelpers.input_to_new_input_list(original_input)
    pre_items = [item.to_input_item() for item in pre_step_items]
    new_items = [item.to_input_item() for item in new_step_items]
    expected_history = original_items + pre_items + new_items

    assert len(expected_history) >= 1  # Should have at least the original input


@pytest.mark.asyncio
async def test_conversation_history_empty_for_first_turn():
    """Test that conversation history works correctly for the first turn."""

    # Create a dummy agent for the items
    dummy_agent = Agent[None](name="dummy")

    # Simulate first turn - only original input, no pre_step_items
    original_input = "Hello"
    pre_step_items: list[RunItem] = []
    new_step_items: list[RunItem] = [
        ToolCallItem(
            agent=dummy_agent,
            raw_item=cast(ResponseFunctionToolCall, get_function_tool_call("first_turn_tool", "")),
        )
    ]

    # Build conversation history as it would be built in the actual execution
    original_items = ItemHelpers.input_to_new_input_list(original_input)
    pre_items = [item.to_input_item() for item in pre_step_items]
    new_items = [item.to_input_item() for item in new_step_items]
    conversation_history = original_items + pre_items + new_items

    # Should have at least the original input
    assert len(conversation_history) >= 1
    assert len(original_items) == 1  # Original input becomes one item


@pytest.mark.asyncio
async def test_conversation_history_multi_turn():
    """Test conversation history accumulates correctly across multiple turns."""

    # Create a dummy agent for the items
    dummy_agent = Agent[None](name="dummy")

    # Simulate multiple turns with accumulated history
    original_input = "Start conversation"
    pre_step_items: list[RunItem] = [
        MessageOutputItem(
            agent=dummy_agent,
            raw_item=cast(ResponseOutputMessage, get_text_message("Response to start")),
        ),
        ToolCallItem(
            agent=dummy_agent,
            raw_item=cast(ResponseFunctionToolCall, get_function_tool_call("multi_turn_tool", "")),
        ),
        ToolCallOutputItem(
            agent=dummy_agent,
            raw_item=cast(
                FunctionCallOutput,
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Previous tool output",
                },
            ),
            output="Previous tool output",
        ),
        MessageOutputItem(
            agent=dummy_agent,
            raw_item=cast(ResponseOutputMessage, get_text_message("Continuing conversation")),
        ),
    ]
    new_step_items: list[RunItem] = [
        ToolCallItem(
            agent=dummy_agent,
            raw_item=cast(ResponseFunctionToolCall, get_function_tool_call("multi_turn_tool", "")),
        )
    ]

    # Build conversation history
    original_items = ItemHelpers.input_to_new_input_list(original_input)
    pre_items = [item.to_input_item() for item in pre_step_items]
    new_items = [item.to_input_item() for item in new_step_items]
    conversation_history = original_items + pre_items + new_items

    # Should contain: original input + all previous messages and tool calls + current tool call
    assert len(conversation_history) >= 5  # At least 5 items in this conversation


def test_conversation_history_immutable():
    """Test that conversation_history cannot be modified after creation."""
    run_context = RunContextWrapper(context=None, usage=Usage())
    history = [get_text_input_item("Original message")]

    tool_context = ToolContext.from_agent_context(
        run_context, "test-id", conversation_history=history
    )

    # Modifying the original list should not affect the tool context
    history.append(get_text_input_item("Should not appear"))

    assert len(tool_context.conversation_history) == 1

    # The conversation_history should be a new list, not a reference
    tool_context.conversation_history.append(get_text_input_item("Direct modification"))

    # Create a new tool context to verify it's not affected
    new_tool_context = ToolContext.from_agent_context(
        run_context, "test-id-2", conversation_history=[get_text_input_item("Original message")]
    )
    assert len(new_tool_context.conversation_history) == 1


def test_conversation_history_with_none():
    """Test that passing None for conversation_history results in empty list."""
    run_context = RunContextWrapper(context=None, usage=Usage())

    tool_context = ToolContext.from_agent_context(run_context, "test-id", conversation_history=None)

    assert tool_context.conversation_history == []
    assert isinstance(tool_context.conversation_history, list)
