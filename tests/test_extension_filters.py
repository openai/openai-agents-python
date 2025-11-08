from copy import deepcopy
from typing import Any, cast

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


def _as_message(item: TResponseInputItem) -> dict[str, Any]:
    assert isinstance(item, dict)
    role = item.get("role")
    assert isinstance(role, str)
    assert role in {"assistant", "user", "system", "developer"}
    return cast(dict[str, Any], item)


def test_empty_data():
    handoff_input_data = HandoffInputData(
        input_history=(),
        pre_handoff_items=(),
        new_items=(),
        run_context=RunContextWrapper(context=()),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_str_historyonly():
    handoff_input_data = HandoffInputData(
        input_history="Hello",
        pre_handoff_items=(),
        new_items=(),
        run_context=RunContextWrapper(context=()),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_str_history_and_list():
    handoff_input_data = HandoffInputData(
        input_history="Hello",
        pre_handoff_items=(),
        new_items=(_get_message_output_run_item("Hello"),),
        run_context=RunContextWrapper(context=()),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert filtered_data == handoff_input_data


def test_list_history_and_list():
    handoff_input_data = HandoffInputData(
        input_history=(_get_message_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("123"),),
        new_items=(_get_message_output_run_item("World"),),
        run_context=RunContextWrapper(context=()),
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
        run_context=RunContextWrapper(context=()),
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
        run_context=RunContextWrapper(context=()),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 0
    assert len(filtered_data.pre_handoff_items) == 0
    assert len(filtered_data.new_items) == 1


def test_removes_tools_from_new_items_and_history():
    handoff_input_data = HandoffInputData(
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
        run_context=RunContextWrapper(context=()),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 3
    assert len(filtered_data.pre_handoff_items) == 1
    assert len(filtered_data.new_items) == 1


def test_removes_handoffs_from_history():
    handoff_input_data = HandoffInputData(
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
        run_context=RunContextWrapper(context=()),
    )
    filtered_data = remove_all_tools(handoff_input_data)
    assert len(filtered_data.input_history) == 1
    assert len(filtered_data.pre_handoff_items) == 1
    assert len(filtered_data.new_items) == 1


def test_nest_handoff_history_wraps_transcript() -> None:
    data = HandoffInputData(
        input_history=(_get_user_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("Assist reply"),),
        new_items=(
            _get_message_output_run_item("Handoff request"),
            _get_handoff_output_run_item("transfer"),
        ),
        run_context=RunContextWrapper(context=()),
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
    data = HandoffInputData(
        input_history=(),
        pre_handoff_items=(_get_reasoning_output_run_item(),),
        new_items=(),
        run_context=RunContextWrapper(context=()),
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
    first = HandoffInputData(
        input_history=(_get_user_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("First reply"),),
        new_items=(),
        run_context=RunContextWrapper(context=()),
    )

    first_nested = nest_handoff_history(first)
    assert isinstance(first_nested.input_history, tuple)
    summary_message = first_nested.input_history[0]

    follow_up_history: tuple[TResponseInputItem, ...] = (
        summary_message,
        _get_user_input_item("Another question"),
    )

    second = HandoffInputData(
        input_history=follow_up_history,
        pre_handoff_items=(_get_message_output_run_item("Second reply"),),
        new_items=(_get_handoff_output_run_item("transfer"),),
        run_context=RunContextWrapper(context=()),
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
    data = HandoffInputData(
        input_history=(_get_user_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("First reply"),),
        new_items=(_get_message_output_run_item("Second reply"),),
        run_context=RunContextWrapper(context=()),
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
    data = HandoffInputData(
        input_history=(_get_user_input_item("Hello"),),
        pre_handoff_items=(_get_message_output_run_item("Assist reply"),),
        new_items=(),
        run_context=RunContextWrapper(context=()),
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
