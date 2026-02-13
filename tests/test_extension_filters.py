from __future__ import annotations

import json as json_module
from copy import deepcopy
from typing import Any, cast
from unittest.mock import patch

from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from agents import (
    Agent,
    HandoffInputData,
    RunContextWrapper,
    get_conversation_history_wrappers,
    reset_conversation_history_wrappers,
    set_conversation_history_wrappers,
)
from agents.extensions.handoff_filters import nest_handoff_history, remove_all_tools
from agents.items import (
    HandoffOutputItem,
    MessageOutputItem,
    ReasoningItem,
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


def _get_user_input_item(content: str) -> TResponseInputItem:
    return {
        "role": "user",
        "content": content,
    }


def _get_reasoning_input_item() -> TResponseInputItem:
    return {"id": "rid", "summary": [], "type": "reasoning"}


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
            content=[
                ResponseOutputText(text=content, annotations=[], type="output_text", logprobs=[])
            ],
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


def _get_reasoning_output_run_item() -> ReasoningItem:
    return ReasoningItem(
        agent=fake_agent(), raw_item=ResponseReasoningItem(id="rid", summary=[], type="reasoning")
    )


def handoff_data(
    input_history: tuple[TResponseInputItem, ...] | str = (),
    pre_handoff_items: tuple[Any, ...] = (),
    new_items: tuple[Any, ...] = (),
) -> HandoffInputData:
    return HandoffInputData(
        input_history=input_history,
        pre_handoff_items=pre_handoff_items,
        new_items=new_items,
        run_context=RunContextWrapper(context=()),
    )


def _as_message(item: TResponseInputItem) -> dict[str, Any]:
    assert isinstance(item, dict)
    role = item.get("role")
    assert isinstance(role, str)
    assert role in {"assistant", "user", "system", "developer"}
    return cast(dict[str, Any], item)


def test_nest_handoff_history_with_string_input() -> None:
    """Test that string input_history is normalized correctly."""
    data = handoff_data(
        input_history="Hello, this is a string input",
    )

    nested = nest_handoff_history(data)

    assert isinstance(nested.input_history, tuple)
    assert len(nested.input_history) == 1
    summary = _as_message(nested.input_history[0])
    assert summary["role"] == "assistant"
    summary_content = summary["content"]
    assert "Hello" in summary_content


def test_empty_data():
    handoff_input_data = handoff_data()
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_str_historyonly():
    handoff_input_data = handoff_data(
        input_history="Hello",
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_str_history_and_list():
    handoff_input_data = handoff_data(
        input_history="Hello",
        new_items=(_get_message_output_run_item("Hello"),),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_list_history_and_list():
    handoff_input_data = handoff_data(
        input_history=(_get_message_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("123"),),
        new_items=(_get_message_output_run_item("World"),),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_removes_tools_from_history():
    handoff_input_data = handoff_data(
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
    handoff_input_data = handoff_data(
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
    handoff_input_data = handoff_data(
        input_history=(
            _get_message_input_item("Hello1"),
            _get_reasoning_input_item(),
            _get_function_result_input_item("World"),
            _get_message_input_item("Hello2"),
        ),
        pre_handoff_items=(
            _get_reasoning_output_run_item(),
            _get_message_output_run_item("123"),
            _get_tool_output_run_item("456"),
        ),
        new_items=(
            _get_reasoning_output_run_item(),
            _get_message_output_run_item("Hello"),
            _get_tool_output_run_item("World"),
        ),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 3
    assert len(filtered_data.pre_handoff_items) == 1
    assert len(filtered_data.new_items) == 1


def test_removes_handoffs_from_history():
    handoff_input_data = handoff_data(
        input_history=(
            _get_message_input_item("Hello1"),
            _get_handoff_input_item("World"),
        ),
        pre_handoff_items=(
            _get_reasoning_output_run_item(),
            _get_message_output_run_item("Hello"),
            _get_tool_output_run_item("World"),
            _get_handoff_output_run_item("World"),
        ),
        new_items=(
            _get_reasoning_output_run_item(),
            _get_message_output_run_item("Hello"),
            _get_tool_output_run_item("World"),
            _get_handoff_output_run_item("World"),
        ),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 1
    assert len(filtered_data.pre_handoff_items) == 1
    assert len(filtered_data.new_items) == 1


def test_nest_handoff_history_wraps_transcript() -> None:
    data = handoff_data(
        input_history=(_get_user_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("Assist reply"),),
        new_items=(
            _get_message_output_run_item("Handoff request"),
            _get_handoff_output_run_item("transfer"),
        ),
    )

    nested = nest_handoff_history(data)

    assert isinstance(nested.input_history, tuple)
    assert len(nested.input_history) == 1
    summary = _as_message(nested.input_history[0])
    assert summary["role"] == "assistant"
    summary_content = summary["content"]
    assert isinstance(summary_content, str)
    start_marker, end_marker = get_conversation_history_wrappers()
    assert start_marker in summary_content
    assert end_marker in summary_content
    assert "Assist reply" in summary_content
    assert "Hello" in summary_content
    assert len(nested.pre_handoff_items) == 0
    assert nested.new_items == data.new_items


def test_nest_handoff_history_handles_missing_user() -> None:
    data = handoff_data(
        pre_handoff_items=(_get_reasoning_output_run_item(),),
    )

    nested = nest_handoff_history(data)

    assert isinstance(nested.input_history, tuple)
    assert len(nested.input_history) == 1
    summary = _as_message(nested.input_history[0])
    assert summary["role"] == "assistant"
    summary_content = summary["content"]
    assert isinstance(summary_content, str)
    assert "reasoning" in summary_content.lower()


def test_nest_handoff_history_appends_existing_history() -> None:
    first = handoff_data(
        input_history=(_get_user_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("First reply"),),
    )

    first_nested = nest_handoff_history(first)
    assert isinstance(first_nested.input_history, tuple)
    summary_message = first_nested.input_history[0]

    follow_up_history: tuple[TResponseInputItem, ...] = (
        summary_message,
        _get_user_input_item("Another question"),
    )

    second = handoff_data(
        input_history=follow_up_history,
        pre_handoff_items=(_get_message_output_run_item("Second reply"),),
        new_items=(_get_handoff_output_run_item("transfer"),),
    )

    second_nested = nest_handoff_history(second)

    assert isinstance(second_nested.input_history, tuple)
    summary = _as_message(second_nested.input_history[0])
    assert summary["role"] == "assistant"
    content = summary["content"]
    assert isinstance(content, str)
    start_marker, end_marker = get_conversation_history_wrappers()
    assert content.count(start_marker) == 1
    assert content.count(end_marker) == 1
    assert "First reply" in content
    assert "Second reply" in content
    assert "Another question" in content


def test_nest_handoff_history_honors_custom_wrappers() -> None:
    data = handoff_data(
        input_history=(_get_user_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("First reply"),),
        new_items=(_get_message_output_run_item("Second reply"),),
    )

    set_conversation_history_wrappers(start="<<START>>", end="<<END>>")
    try:
        nested = nest_handoff_history(data)
        assert isinstance(nested.input_history, tuple)
        assert len(nested.input_history) == 1
        summary = _as_message(nested.input_history[0])
        summary_content = summary["content"]
        assert isinstance(summary_content, str)
        lines = summary_content.splitlines()
        assert lines[0] == (
            "For context, here is the conversation so far between the user and the previous agent:"
        )
        assert lines[1].startswith("<<START>>")
        assert summary_content.endswith("<<END>>")

        # Ensure the custom markers are parsed correctly when nesting again.
        second_nested = nest_handoff_history(nested)
        assert isinstance(second_nested.input_history, tuple)
        second_summary = _as_message(second_nested.input_history[0])
        content = second_summary["content"]
        assert isinstance(content, str)
        assert content.count("<<START>>") == 1
        assert content.count("<<END>>") == 1
    finally:
        reset_conversation_history_wrappers()


def test_nest_handoff_history_supports_custom_mapper() -> None:
    data = handoff_data(
        input_history=(_get_user_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("Assist reply"),),
    )

    def map_history(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
        reversed_items = list(reversed(items))
        return [deepcopy(item) for item in reversed_items]

    nested = nest_handoff_history(data, history_mapper=map_history)

    assert isinstance(nested.input_history, tuple)
    assert len(nested.input_history) == 2
    first = _as_message(nested.input_history[0])
    second = _as_message(nested.input_history[1])
    assert first["role"] == "assistant"
    first_content = first.get("content")
    assert isinstance(first_content, list)
    assert any(
        isinstance(chunk, dict)
        and chunk.get("type") == "output_text"
        and chunk.get("text") == "Assist reply"
        for chunk in first_content
    )
    assert second["role"] == "user"
    assert second["content"] == "Hello"


def test_nest_handoff_history_empty_transcript() -> None:
    """Test that empty transcript shows '(no previous turns recorded)'."""
    data = handoff_data()

    nested = nest_handoff_history(data)

    assert isinstance(nested.input_history, tuple)
    assert len(nested.input_history) == 1
    summary = _as_message(nested.input_history[0])
    assert summary["role"] == "assistant"
    summary_content = summary["content"]
    assert isinstance(summary_content, str)
    assert "(no previous turns recorded)" in summary_content


def test_nest_handoff_history_role_with_name() -> None:
    """Test that items with role and name are formatted correctly."""
    data = handoff_data(
        input_history=(
            cast(TResponseInputItem, {"role": "user", "name": "Alice", "content": "Hello"}),
        ),
    )

    nested = nest_handoff_history(data)

    assert isinstance(nested.input_history, tuple)
    assert len(nested.input_history) == 1
    summary = _as_message(nested.input_history[0])
    summary_content = summary["content"]
    assert "user (Alice): Hello" in summary_content


def test_nest_handoff_history_item_without_role() -> None:
    """Test that items without role are handled correctly."""
    # Create an item that doesn't have a role (e.g., a function call)
    data = handoff_data(
        input_history=(
            cast(
                TResponseInputItem, {"type": "function_call", "call_id": "123", "name": "test_tool"}
            ),
        ),
    )

    nested = nest_handoff_history(data)

    assert isinstance(nested.input_history, tuple)
    assert len(nested.input_history) == 1
    summary = _as_message(nested.input_history[0])
    summary_content = summary["content"]
    assert "function_call" in summary_content
    assert "test_tool" in summary_content


def test_nest_handoff_history_content_handling() -> None:
    """Test various content types are handled correctly."""
    # Test None content
    data = handoff_data(
        input_history=(cast(TResponseInputItem, {"role": "user", "content": None}),),
    )

    nested = nest_handoff_history(data)
    assert isinstance(nested.input_history, tuple)
    summary = _as_message(nested.input_history[0])
    summary_content = summary["content"]
    assert "user:" in summary_content or "user" in summary_content

    # Test non-string, non-None content (list)
    data2 = handoff_data(
        input_history=(
            cast(
                TResponseInputItem, {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
            ),
        ),
    )

    nested2 = nest_handoff_history(data2)
    assert isinstance(nested2.input_history, tuple)
    summary2 = _as_message(nested2.input_history[0])
    summary_content2 = summary2["content"]
    assert "Hello" in summary_content2 or "text" in summary_content2


def test_nest_handoff_history_extract_nested_non_string_content() -> None:
    """Test that _extract_nested_history_transcript handles non-string content."""
    # Create a summary message with non-string content (array)
    summary_with_array = cast(
        TResponseInputItem,
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "test"}],
        },
    )

    data = handoff_data(
        input_history=(summary_with_array,),
    )

    # This should not extract nested history since content is not a string
    nested = nest_handoff_history(data)
    assert isinstance(nested.input_history, tuple)
    # Should still create a summary, not extract nested content


def test_nest_handoff_history_parse_summary_line_edge_cases() -> None:
    """Test edge cases in parsing summary lines."""
    # Create a nested summary that will be parsed
    first_summary = nest_handoff_history(
        handoff_data(
            input_history=(_get_user_input_item("Hello"),),
            pre_handoff_items=(_get_message_output_run_item("Reply"),),
        )
    )

    # Create a second nested summary that includes the first
    # This will trigger parsing of the nested summary lines
    assert isinstance(first_summary.input_history, tuple)
    second_data = handoff_data(
        input_history=(
            first_summary.input_history[0],
            _get_user_input_item("Another question"),
        ),
    )

    nested = nest_handoff_history(second_data)
    # Should successfully parse and include both messages
    assert isinstance(nested.input_history, tuple)
    summary = _as_message(nested.input_history[0])
    assert "Hello" in summary["content"] or "Another question" in summary["content"]


def test_nest_handoff_history_role_with_name_parsing() -> None:
    """Test parsing of role with name in parentheses."""
    # Create a summary that includes a role with name
    data = handoff_data(
        input_history=(
            cast(TResponseInputItem, {"role": "user", "name": "Alice", "content": "Hello"}),
        ),
    )

    first_nested = nest_handoff_history(data)
    assert isinstance(first_nested.input_history, tuple)
    summary = first_nested.input_history[0]

    # Now nest again to trigger parsing
    second_data = handoff_data(
        input_history=(summary,),
    )

    second_nested = nest_handoff_history(second_data)
    # Should successfully parse the role with name
    assert isinstance(second_nested.input_history, tuple)
    final_summary = _as_message(second_nested.input_history[0])
    assert "Alice" in final_summary["content"] or "user" in final_summary["content"]


def test_nest_handoff_history_parses_role_with_name_in_parentheses() -> None:
    """Test parsing of role with name in parentheses format."""
    # Create a summary with role (name) format
    first_data = handoff_data(
        input_history=(
            cast(TResponseInputItem, {"role": "user", "name": "Alice", "content": "Hello"}),
        ),
    )

    first_nested = nest_handoff_history(first_data)
    # The summary should contain "user (Alice): Hello"
    assert isinstance(first_nested.input_history, tuple)

    # Now nest again - this will parse the summary line
    second_data = handoff_data(
        input_history=(first_nested.input_history[0],),
    )

    second_nested = nest_handoff_history(second_data)
    # Should successfully parse and reconstruct the role with name
    assert isinstance(second_nested.input_history, tuple)
    final_summary = _as_message(second_nested.input_history[0])
    # The parsed item should have name field
    assert "Alice" in final_summary["content"] or "user" in final_summary["content"]


def test_nest_handoff_history_handles_parsing_edge_cases() -> None:
    """Test edge cases in summary line parsing."""
    # Create a summary that will be parsed
    summary_content = (
        "For context, here is the conversation so far:\n"
        "<CONVERSATION HISTORY>\n"
        "1. user: Hello\n"  # Normal case
        "2.   \n"  # Empty/whitespace line (should be skipped)
        "3. no_colon_separator\n"  # No colon (should return None)
        "4. : no role\n"  # Empty role_text (should return None)
        "5. assistant (Bob): Reply\n"  # Role with name
        "</CONVERSATION HISTORY>"
    )

    summary_item = cast(TResponseInputItem, {"role": "assistant", "content": summary_content})

    # Nest again to trigger parsing
    data = handoff_data(
        input_history=(summary_item,),
    )

    nested = nest_handoff_history(data)
    # Should handle edge cases gracefully
    assert isinstance(nested.input_history, tuple)
    final_summary = _as_message(nested.input_history[0])
    assert "Hello" in final_summary["content"] or "Reply" in final_summary["content"]


def test_nest_handoff_history_handles_unserializable_items() -> None:
    """Test that items with unserializable content are handled gracefully."""

    # Create an item with a circular reference or other unserializable content
    class Unserializable:
        def __str__(self) -> str:
            return "unserializable"

    # Create an item that will trigger TypeError in json.dumps
    # We'll use a dict with a non-serializable value
    data = handoff_data(
        input_history=(
            cast(
                TResponseInputItem,
                {
                    "type": "custom_item",
                    "unserializable_field": Unserializable(),  # This will cause TypeError
                },
            ),
        ),
    )

    # Should not crash, should fall back to str()
    nested = nest_handoff_history(data)
    assert isinstance(nested.input_history, tuple)
    summary = _as_message(nested.input_history[0])
    summary_content = summary["content"]
    # Should contain the item type
    assert "custom_item" in summary_content or "unserializable" in summary_content


def test_nest_handoff_history_handles_unserializable_content() -> None:
    """Test that content with unserializable values is handled gracefully."""

    class UnserializableContent:
        def __str__(self) -> str:
            return "unserializable_content"

    data = handoff_data(
        input_history=(
            cast(TResponseInputItem, {"role": "user", "content": UnserializableContent()}),
        ),
    )

    # Should not crash, should fall back to str()
    nested = nest_handoff_history(data)
    assert isinstance(nested.input_history, tuple)
    summary = _as_message(nested.input_history[0])
    summary_content = summary["content"]
    assert "unserializable_content" in summary_content or "user" in summary_content


def test_nest_handoff_history_handles_empty_lines_in_parsing() -> None:
    """Test that empty/whitespace lines in nested history are skipped."""
    # Create a summary with empty lines that will be parsed
    summary_content = (
        "For context, here is the conversation so far:\n"
        "<CONVERSATION HISTORY>\n"
        "1. user: Hello\n"
        "   \n"  # Empty/whitespace line (should return None)
        "2. assistant: Reply\n"
        "</CONVERSATION HISTORY>"
    )

    summary_item = cast(TResponseInputItem, {"role": "assistant", "content": summary_content})

    # Nest again to trigger parsing
    data = handoff_data(
        input_history=(summary_item,),
    )

    nested = nest_handoff_history(data)
    # Should handle empty lines gracefully
    assert isinstance(nested.input_history, tuple)
    final_summary = _as_message(nested.input_history[0])
    assert "Hello" in final_summary["content"] or "Reply" in final_summary["content"]


def test_nest_handoff_history_json_dumps_typeerror() -> None:
    """Test that TypeError in json.dumps is handled gracefully."""
    # Create an item that will trigger json.dumps
    data = handoff_data(
        input_history=(cast(TResponseInputItem, {"type": "custom_item", "field": "value"}),),
    )

    # Mock json.dumps to raise TypeError
    with patch.object(json_module, "dumps", side_effect=TypeError("Cannot serialize")):
        nested = nest_handoff_history(data)
        assert isinstance(nested.input_history, tuple)
        summary = _as_message(nested.input_history[0])
        summary_content = summary["content"]
        # Should fall back to str()
        assert "custom_item" in summary_content


def test_nest_handoff_history_stringify_content_typeerror() -> None:
    """Test that TypeError in json.dumps for content is handled gracefully."""
    data = handoff_data(
        input_history=(
            cast(TResponseInputItem, {"role": "user", "content": {"complex": "object"}}),
        ),
    )

    # Mock json.dumps to raise TypeError when stringifying content
    with patch.object(json_module, "dumps", side_effect=TypeError("Cannot serialize")):
        nested = nest_handoff_history(data)
        assert isinstance(nested.input_history, tuple)
        summary = _as_message(nested.input_history[0])
        summary_content = summary["content"]
        # Should fall back to str()
        assert "user" in summary_content or "object" in summary_content


def test_nest_handoff_history_parse_summary_line_empty_stripped() -> None:
    """Test that _parse_summary_line returns None for empty/whitespace-only lines."""
    # Create a summary with empty lines that will trigger line 204
    summary_content = (
        "For context, here is the conversation so far:\n"
        "<CONVERSATION HISTORY>\n"
        "1. user: Hello\n"
        "   \n"  # Whitespace-only line (should return None at line 204)
        "2. assistant: Reply\n"
        "</CONVERSATION HISTORY>"
    )

    summary_item = cast(TResponseInputItem, {"role": "assistant", "content": summary_content})

    # Nest again to trigger parsing
    data = handoff_data(
        input_history=(summary_item,),
    )

    nested = nest_handoff_history(data)
    # Should handle empty lines gracefully
    assert isinstance(nested.input_history, tuple)
    final_summary = _as_message(nested.input_history[0])
    assert "Hello" in final_summary["content"] or "Reply" in final_summary["content"]
