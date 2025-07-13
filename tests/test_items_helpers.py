from __future__ import annotations

from openai.types.responses.response_computer_tool_call import (
    ActionScreenshot,
    ResponseComputerToolCall,
)
from openai.types.responses.response_computer_tool_call_param import ResponseComputerToolCallParam
from openai.types.responses.response_file_search_tool_call import ResponseFileSearchToolCall
from openai.types.responses.response_file_search_tool_call_param import (
    ResponseFileSearchToolCallParam,
)
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from openai.types.responses.response_function_tool_call_param import ResponseFunctionToolCallParam
from openai.types.responses.response_function_web_search import (
    ActionSearch,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_function_web_search_param import ResponseFunctionWebSearchParam
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_message_param import ResponseOutputMessageParam
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary
from openai.types.responses.response_reasoning_item_param import ResponseReasoningItemParam

from agents import (
    Agent,
    ItemHelpers,
    MessageOutputItem,
    ModelResponse,
    ReasoningItem,
    RunItem,
    TResponseInputItem,
    Usage,
)


def make_message(
    content_items: list[ResponseOutputText | ResponseOutputRefusal],
) -> ResponseOutputMessage:
    """
    Helper to construct a ResponseOutputMessage with a single batch of content
    items, using a fixed id/status.
    """
    return ResponseOutputMessage(
        id="msg123",
        content=content_items,
        role="assistant",
        status="completed",
        type="message",
    )


def test_extract_last_content_of_text_message() -> None:
    # Build a message containing two text segments.
    content1 = ResponseOutputText(annotations=[], text="Hello ", type="output_text")
    content2 = ResponseOutputText(annotations=[], text="world!", type="output_text")
    message = make_message([content1, content2])
    # Helpers should yield the last segment's text.
    assert ItemHelpers.extract_last_content(message) == "world!"


def test_extract_last_content_of_refusal_message() -> None:
    # Build a message whose last content entry is a refusal.
    content1 = ResponseOutputText(annotations=[], text="Before refusal", type="output_text")
    refusal = ResponseOutputRefusal(refusal="I cannot do that", type="refusal")
    message = make_message([content1, refusal])
    # Helpers should extract the refusal string when last content is a refusal.
    assert ItemHelpers.extract_last_content(message) == "I cannot do that"


def test_extract_last_content_non_message_returns_empty() -> None:
    # Construct some other type of output item, e.g. a tool call, to verify non-message returns "".
    tool_call = ResponseFunctionToolCall(
        id="tool123",
        arguments="{}",
        call_id="call123",
        name="func",
        type="function_call",
    )
    assert ItemHelpers.extract_last_content(tool_call) == ""


def test_extract_last_text_returns_text_only() -> None:
    # A message whose last segment is text yields the text.
    first_text = ResponseOutputText(annotations=[], text="part1", type="output_text")
    second_text = ResponseOutputText(annotations=[], text="part2", type="output_text")
    message = make_message([first_text, second_text])
    assert ItemHelpers.extract_last_text(message) == "part2"
    # Whereas when last content is a refusal, extract_last_text returns None.
    message2 = make_message([first_text, ResponseOutputRefusal(refusal="no", type="refusal")])
    assert ItemHelpers.extract_last_text(message2) is None


def test_input_to_new_input_list_from_string() -> None:
    result = ItemHelpers.input_to_new_input_list("hi")
    # Should wrap the string into a list with a single dict containing content and user role.
    assert isinstance(result, list)
    assert result == [{"content": "hi", "role": "user"}]


def test_input_to_new_input_list_deep_copies_lists() -> None:
    # Given a list of message dictionaries, ensure the returned list is a deep copy.
    original: list[TResponseInputItem] = [{"content": "abc", "role": "developer"}]
    new_list = ItemHelpers.input_to_new_input_list(original)
    assert new_list == original
    # Mutating the returned list should not mutate the original.
    new_list.pop()
    assert "content" in original[0] and original[0].get("content") == "abc"


def test_text_message_output_concatenates_text_segments() -> None:
    # Build a message with both text and refusal segments, only text segments are concatenated.
    pieces: list[ResponseOutputText | ResponseOutputRefusal] = []
    pieces.append(ResponseOutputText(annotations=[], text="a", type="output_text"))
    pieces.append(ResponseOutputRefusal(refusal="denied", type="refusal"))
    pieces.append(ResponseOutputText(annotations=[], text="b", type="output_text"))
    message = make_message(pieces)
    # Wrap into MessageOutputItem to feed into text_message_output.
    item = MessageOutputItem(agent=Agent(name="test"), raw_item=message)
    assert ItemHelpers.text_message_output(item) == "ab"


def test_text_message_outputs_across_list_of_runitems() -> None:
    """
    Compose several RunItem instances, including a non-message run item, and ensure
    that only MessageOutputItem instances contribute any text. The non-message
    (ReasoningItem) should be ignored by Helpers.text_message_outputs.
    """
    message1 = make_message([ResponseOutputText(annotations=[], text="foo", type="output_text")])
    message2 = make_message([ResponseOutputText(annotations=[], text="bar", type="output_text")])
    item1: RunItem = MessageOutputItem(agent=Agent(name="test"), raw_item=message1)
    item2: RunItem = MessageOutputItem(agent=Agent(name="test"), raw_item=message2)
    # Create a non-message run item of a different type, e.g., a reasoning trace.
    reasoning = ResponseReasoningItem(id="rid", summary=[], type="reasoning")
    non_message_item: RunItem = ReasoningItem(agent=Agent(name="test"), raw_item=reasoning)
    # Confirm only the message outputs are concatenated.
    assert ItemHelpers.text_message_outputs([item1, non_message_item, item2]) == "foobar"


def test_tool_call_output_item_constructs_function_call_output_dict():
    # Build a simple ResponseFunctionToolCall.
    call = ResponseFunctionToolCall(
        id="call-abc",
        arguments='{"x": 1}',
        call_id="call-abc",
        name="do_something",
        type="function_call",
    )
    payload = ItemHelpers.tool_call_output_item(call, "result-string")

    assert isinstance(payload, dict)
    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.id
    assert payload["output"] == "result-string"


# The following tests ensure that every possible output item type defined by
# OpenAI's API can be converted back into an input item dict via
# ModelResponse.to_input_items. The output and input schema for each item are
# intended to be symmetric, so given any ResponseOutputItem, its model_dump
# should produce a dict that can satisfy the corresponding TypedDict input
# type. These tests construct minimal valid instances of each output type,
# invoke to_input_items, and then verify that the resulting dict can be used
# to round-trip back into a Pydantic output model without errors.


def test_to_input_items_for_message() -> None:
    """An output message should convert into an input dict matching the message's own structure."""
    content = ResponseOutputText(annotations=[], text="hello world", type="output_text")
    message = ResponseOutputMessage(
        id="m1", content=[content], role="assistant", status="completed", type="message"
    )
    resp = ModelResponse(output=[message], usage=Usage(), response_id=None)
    input_items = resp.to_input_items()
    assert isinstance(input_items, list) and len(input_items) == 1
    # The dict should contain exactly the primitive values of the message
    expected: ResponseOutputMessageParam = {
        "id": "m1",
        "content": [
            {
                "annotations": [],
                "text": "hello world",
                "type": "output_text",
            }
        ],
        "role": "assistant",
        "status": "completed",
        "type": "message",
    }
    assert input_items[0] == expected


def test_to_input_items_for_function_call() -> None:
    """A function tool call output should produce the same dict as a function tool call input."""
    tool_call = ResponseFunctionToolCall(
        id="f1", arguments="{}", call_id="c1", name="func", type="function_call"
    )
    resp = ModelResponse(output=[tool_call], usage=Usage(), response_id=None)
    input_items = resp.to_input_items()
    assert isinstance(input_items, list) and len(input_items) == 1
    expected: ResponseFunctionToolCallParam = {
        "id": "f1",
        "arguments": "{}",
        "call_id": "c1",
        "name": "func",
        "type": "function_call",
    }
    assert input_items[0] == expected


def test_to_input_items_for_file_search_call() -> None:
    """A file search tool call output should produce the same dict as a file search input."""
    fs_call = ResponseFileSearchToolCall(
        id="fs1", queries=["query"], status="completed", type="file_search_call"
    )
    resp = ModelResponse(output=[fs_call], usage=Usage(), response_id=None)
    input_items = resp.to_input_items()
    assert isinstance(input_items, list) and len(input_items) == 1
    expected: ResponseFileSearchToolCallParam = {
        "id": "fs1",
        "queries": ["query"],
        "status": "completed",
        "type": "file_search_call",
    }
    assert input_items[0] == expected


def test_to_input_items_for_web_search_call() -> None:
    """A web search tool call output should produce the same dict as a web search input."""
    ws_call = ResponseFunctionWebSearch(
        id="w1",
        action=ActionSearch(type="search", query="query"),
        status="completed",
        type="web_search_call",
    )
    resp = ModelResponse(output=[ws_call], usage=Usage(), response_id=None)
    input_items = resp.to_input_items()
    assert isinstance(input_items, list) and len(input_items) == 1
    expected: ResponseFunctionWebSearchParam = {
        "id": "w1",
        "status": "completed",
        "type": "web_search_call",
        "action": {"type": "search", "query": "query"},
    }
    assert input_items[0] == expected


def test_to_input_items_for_computer_call_click() -> None:
    """A computer call output should yield a dict whose shape matches the computer call input."""
    action = ActionScreenshot(type="screenshot")
    comp_call = ResponseComputerToolCall(
        id="comp1",
        action=action,
        type="computer_call",
        call_id="comp1",
        pending_safety_checks=[],
        status="completed",
    )
    resp = ModelResponse(output=[comp_call], usage=Usage(), response_id=None)
    input_items = resp.to_input_items()
    assert isinstance(input_items, list) and len(input_items) == 1
    converted_dict = input_items[0]
    # Top-level keys should match what we expect for a computer call input
    expected: ResponseComputerToolCallParam = {
        "id": "comp1",
        "type": "computer_call",
        "action": {"type": "screenshot"},
        "call_id": "comp1",
        "pending_safety_checks": [],
        "status": "completed",
    }
    assert converted_dict == expected


def test_to_input_items_for_reasoning() -> None:
    """A reasoning output should produce the same dict as a reasoning input item."""
    rc = Summary(text="why", type="summary_text")
    reasoning = ResponseReasoningItem(id="rid1", summary=[rc], type="reasoning")
    resp = ModelResponse(output=[reasoning], usage=Usage(), response_id=None)
    input_items = resp.to_input_items()
    assert isinstance(input_items, list) and len(input_items) == 1
    converted_dict = input_items[0]

    expected: ResponseReasoningItemParam = {
        "id": "rid1",
        "summary": [{"text": "why", "type": "summary_text"}],
        "type": "reasoning",
    }
    print(converted_dict)
    print(expected)
    assert converted_dict == expected
