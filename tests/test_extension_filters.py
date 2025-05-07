import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents import Agent, HandoffInputData
from agents.extensions.handoff_filters import remove_all_tools, keep_last_n_items
from agents.items import (
    HandoffOutputItem,
    MessageOutputItem,
    ToolCallOutputItem,
    TResponseInputItem,
)


def fake_agent():
    return Agent(
        name="fake_agent",
    )


def _get_message_input_item(content: str) -> TResponseInputItem:
    return {
        "role": "assistant",
        "content": content,
    }


def _get_function_result_input_item(content: str) -> TResponseInputItem:
    return {
        "call_id": "1",
        "output": content,
        "type": "function_call_output",
    }


def _get_message_output_run_item(content: str) -> MessageOutputItem:
    return MessageOutputItem(
        agent=fake_agent(),
        raw_item=ResponseOutputMessage(
            id="1",
            content=[ResponseOutputText(text=content, annotations=[], type="output_text")],
            role="assistant",
            status="completed",
            type="message",
        ),
    )


def _get_tool_output_run_item(content: str) -> ToolCallOutputItem:
    return ToolCallOutputItem(
        agent=fake_agent(),
        raw_item={
            "call_id": "1",
            "output": content,
            "type": "function_call_output",
        },
        output=content,
    )


def _get_handoff_input_item(content: str) -> TResponseInputItem:
    return {
        "call_id": "1",
        "output": content,
        "type": "function_call_output",
    }


def _get_handoff_output_run_item(content: str) -> HandoffOutputItem:
    return HandoffOutputItem(
        agent=fake_agent(),
        raw_item={
            "call_id": "1",
            "output": content,
            "type": "function_call_output",
        },
        source_agent=fake_agent(),
        target_agent=fake_agent(),
    )


def test_empty_data():
    handoff_input_data = HandoffInputData(input_history=(), pre_handoff_items=(), new_items=())
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_str_historyonly():
    handoff_input_data = HandoffInputData(input_history="Hello", pre_handoff_items=(), new_items=())
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_str_history_and_list():
    handoff_input_data = HandoffInputData(
        input_history="Hello",
        pre_handoff_items=(),
        new_items=(_get_message_output_run_item("Hello"),),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_list_history_and_list():
    handoff_input_data = HandoffInputData(
        input_history=(_get_message_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("123"),),
        new_items=(_get_message_output_run_item("World"),),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_removes_tools_from_history():
    handoff_input_data = HandoffInputData(
        input_history=(
            _get_message_input_item("Hello1"),
            _get_function_result_input_item("World"),
            _get_message_input_item("Hello2"),
        ),
        pre_handoff_items=(
            _get_tool_output_run_item("abc"),
            _get_message_output_run_item("123"),
        ),
        new_items=(_get_message_output_run_item("World"),),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 2
    assert len(filtered_data.pre_handoff_items) == 1
    assert len(filtered_data.new_items) == 1


def test_removes_tools_from_new_items():
    handoff_input_data = HandoffInputData(
        input_history=(),
        pre_handoff_items=(),
        new_items=(
            _get_message_output_run_item("Hello"),
            _get_tool_output_run_item("World"),
        ),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 0
    assert len(filtered_data.pre_handoff_items) == 0
    assert len(filtered_data.new_items) == 1


def test_removes_tools_from_new_items_and_history():
    handoff_input_data = HandoffInputData(
        input_history=(
            _get_message_input_item("Hello1"),
            _get_function_result_input_item("World"),
            _get_message_input_item("Hello2"),
        ),
        pre_handoff_items=(
            _get_message_output_run_item("123"),
            _get_tool_output_run_item("456"),
        ),
        new_items=(
            _get_message_output_run_item("Hello"),
            _get_tool_output_run_item("World"),
        ),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 2
    assert len(filtered_data.pre_handoff_items) == 1
    assert len(filtered_data.new_items) == 1


def test_removes_handoffs_from_history():
    handoff_input_data = HandoffInputData(
        input_history=(
            _get_message_input_item("Hello1"),
            _get_handoff_input_item("World"),
        ),
        pre_handoff_items=(
            _get_message_output_run_item("Hello"),
            _get_tool_output_run_item("World"),
            _get_handoff_output_run_item("World"),
        ),
        new_items=(
            _get_message_output_run_item("Hello"),
            _get_tool_output_run_item("World"),
            _get_handoff_output_run_item("World"),
        ),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 1
    assert len(filtered_data.pre_handoff_items) == 1
    assert len(filtered_data.new_items) == 1


def test_keep_last_n_items_basic():
    """Test the basic functionality of keep_last_n_items."""
    handoff_input_data = HandoffInputData(
        input_history=(
            _get_message_input_item("Message 1"),
            _get_message_input_item("Message 2"),
            _get_message_input_item("Message 3"),
            _get_message_input_item("Message 4"),
            _get_message_input_item("Message 5"),
        ),
        pre_handoff_items=(_get_message_output_run_item("Pre handoff"),),
        new_items=(_get_message_output_run_item("New item"),),
    )

    # Keep last 2 items
    filtered_data = keep_last_n_items(handoff_input_data, 2)

    assert len(filtered_data.input_history) == 2
    assert filtered_data.input_history[-1] == _get_message_input_item("Message 5")
    assert filtered_data.input_history[-2] == _get_message_input_item("Message 4")

    # Pre-handoff and new items should remain unchanged
    assert len(filtered_data.pre_handoff_items) == 1
    assert len(filtered_data.new_items) == 1


def test_keep_last_n_items_with_tool_messages():
    """Test keeping last N items while removing tool messages."""
    handoff_input_data = HandoffInputData(
        input_history=(
            _get_message_input_item("Message 1"),
            _get_function_result_input_item("Function result"),
            _get_message_input_item("Message 2"),
            _get_handoff_input_item("Handoff"),
            _get_message_input_item("Message 3"),
        ),
        pre_handoff_items=(_get_message_output_run_item("Pre handoff"),),
        new_items=(_get_message_output_run_item("New item"),),
    )

    # Keep last 2 items but remove tool messages first
    filtered_data = keep_last_n_items(handoff_input_data, 2, keep_tool_messages=False)

    # Should have the last 2 non-tool messages
    assert len(filtered_data.input_history) == 2
    assert filtered_data.input_history[-1] == _get_message_input_item("Message 3")
    assert filtered_data.input_history[-2] == _get_message_input_item("Message 2")


def test_keep_last_n_items_all():
    """Test keeping more items than exist."""
    handoff_input_data = HandoffInputData(
        input_history=(
            _get_message_input_item("Message 1"),
            _get_message_input_item("Message 2"),
        ),
        pre_handoff_items=(_get_message_output_run_item("Pre handoff"),),
        new_items=(_get_message_output_run_item("New item"),),
    )

    # Request more items than exist
    filtered_data = keep_last_n_items(handoff_input_data, 10)

    # Should keep all items
    assert len(filtered_data.input_history) == 2
    assert filtered_data.input_history == handoff_input_data.input_history


def test_keep_last_n_items_with_string_history():
    """Test handling of string input_history."""
    handoff_input_data = HandoffInputData(
        input_history="This is a string history",
        pre_handoff_items=(_get_message_output_run_item("Pre handoff"),),
        new_items=(_get_message_output_run_item("New item"),),
    )

    # String history should be preserved
    filtered_data = keep_last_n_items(handoff_input_data, 3)

    assert filtered_data.input_history == "This is a string history"


def test_keep_last_n_items_invalid_input():
    """Test error handling for invalid inputs."""
    handoff_input_data = HandoffInputData(
        input_history=(_get_message_input_item("Message 1"),),
        pre_handoff_items=(),
        new_items=(),
    )

    # Test with invalid n values
    with pytest.raises(ValueError, match="n must be a positive integer"):
        keep_last_n_items(handoff_input_data, 0)

    with pytest.raises(ValueError, match="n must be a positive integer"):
        keep_last_n_items(handoff_input_data, -5)

    with pytest.raises(ValueError, match="n must be an integer"):
        keep_last_n_items(handoff_input_data, "3")


def test_keep_last_n_items_empty_history():
    """Test with an empty input history."""
    handoff_input_data = HandoffInputData(
        input_history=(),
        pre_handoff_items=(_get_message_output_run_item("Pre handoff"),),
        new_items=(_get_message_output_run_item("New item"),),
    )

    # Empty history should remain empty
    filtered_data = keep_last_n_items(handoff_input_data, 3)

    assert len(filtered_data.input_history) == 0
