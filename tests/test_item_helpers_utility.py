"""Tests for ItemHelpers utility methods."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.items import (
    HandoffCallItem,
    HandoffOutputItem,
    ItemHelpers,
    MessageOutputItem,
    ToolCallItem,
    ToolCallOutputItem,
)


def create_mock_agent() -> MagicMock:
    """Create a mock agent for testing."""
    agent = MagicMock()
    agent.name = "TestAgent"
    return agent


class TestItemHelpersUtilityMethods:
    """Tests for ItemHelpers utility methods."""

    def test_filter_by_type_single_type(self) -> None:
        """Filter by a single type should return matching items."""
        agent = create_mock_agent()
        msg_item = MagicMock(spec=MessageOutputItem)
        msg_item.type = "message_output_item"
        tool_item = MagicMock(spec=ToolCallItem)
        tool_item.type = "tool_call_item"

        items = [msg_item, tool_item, msg_item]
        result = ItemHelpers.filter_by_type(items, MessageOutputItem)

        assert len(result) == 2
        for item in result:
            assert isinstance(item, MessageOutputItem)

    def test_filter_by_type_tuple_of_types(self) -> None:
        """Filter by tuple of types should return all matching items."""
        msg_item = MagicMock(spec=MessageOutputItem)
        tool_item = MagicMock(spec=ToolCallItem)
        output_item = MagicMock(spec=ToolCallOutputItem)

        items = [msg_item, tool_item, output_item]
        result = ItemHelpers.filter_by_type(items, (ToolCallItem, ToolCallOutputItem))

        assert len(result) == 2

    def test_filter_by_type_empty_list(self) -> None:
        """Filter on empty list should return empty list."""
        result = ItemHelpers.filter_by_type([], MessageOutputItem)
        assert result == []

    def test_count_by_type_mixed_items(self) -> None:
        """Count should return correct counts per type."""
        msg1 = MagicMock(spec=MessageOutputItem)
        msg1.type = "message_output_item"
        msg2 = MagicMock(spec=MessageOutputItem)
        msg2.type = "message_output_item"
        tool = MagicMock(spec=ToolCallItem)
        tool.type = "tool_call_item"

        items = [msg1, msg2, tool]
        counts = ItemHelpers.count_by_type(items)

        assert counts["message_output_item"] == 2
        assert counts["tool_call_item"] == 1

    def test_count_by_type_empty_list(self) -> None:
        """Count on empty list should return empty dict."""
        counts = ItemHelpers.count_by_type([])
        assert counts == {}

    def test_has_handoffs_true(self) -> None:
        """has_handoffs should return True when handoff items present."""
        msg = MagicMock(spec=MessageOutputItem)
        handoff = MagicMock(spec=HandoffCallItem)

        assert ItemHelpers.has_handoffs([msg, handoff]) is True

    def test_has_handoffs_with_output_item(self) -> None:
        """has_handoffs should return True for HandoffOutputItem."""
        msg = MagicMock(spec=MessageOutputItem)
        handoff_out = MagicMock(spec=HandoffOutputItem)

        assert ItemHelpers.has_handoffs([msg, handoff_out]) is True

    def test_has_handoffs_false(self) -> None:
        """has_handoffs should return False when no handoffs present."""
        msg = MagicMock(spec=MessageOutputItem)
        tool = MagicMock(spec=ToolCallItem)

        assert ItemHelpers.has_handoffs([msg, tool]) is False

    def test_has_handoffs_empty_list(self) -> None:
        """has_handoffs should return False for empty list."""
        assert ItemHelpers.has_handoffs([]) is False

    def test_get_tool_calls(self) -> None:
        """get_tool_calls should extract only ToolCallItem instances."""
        msg = MagicMock(spec=MessageOutputItem)
        tool1 = MagicMock(spec=ToolCallItem)
        tool2 = MagicMock(spec=ToolCallItem)
        output = MagicMock(spec=ToolCallOutputItem)

        items = [msg, tool1, output, tool2]
        result = ItemHelpers.get_tool_calls(items)

        assert len(result) == 2
        for item in result:
            assert isinstance(item, ToolCallItem)

    def test_get_messages(self) -> None:
        """get_messages should extract only MessageOutputItem instances."""
        msg1 = MagicMock(spec=MessageOutputItem)
        msg2 = MagicMock(spec=MessageOutputItem)
        tool = MagicMock(spec=ToolCallItem)

        items = [msg1, tool, msg2]
        result = ItemHelpers.get_messages(items)

        assert len(result) == 2
        for item in result:
            assert isinstance(item, MessageOutputItem)
