import json
from typing import Any, cast

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents import Agent
from agents.items import (
    MessageOutputItem,
    ModelResponse,
    RunItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from agents.run import _ServerConversationTracker
from agents.usage import Usage


class DummyRunItem:
    """Minimal stand-in for RunItem with the attributes used by _ServerConversationTracker."""

    def __init__(self, raw_item: dict[str, Any], type: str = "message") -> None:
        self.raw_item = raw_item
        self.type = type


def test_prepare_input_filters_items_seen_by_server_and_tool_calls() -> None:
    tracker = _ServerConversationTracker(conversation_id="conv", previous_response_id=None)

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

    tracker.prime_from_state(
        original_input=original_input,
        generated_items=generated_items,  # type: ignore[arg-type]
        model_responses=[model_response],
        session_items=session_items,
    )

    prepared = tracker.prepare_input(
        original_input=original_input,
        generated_items=generated_items,  # type: ignore[arg-type]
        model_responses=[model_response],
    )

    assert prepared == [new_raw_item]
    assert tracker.sent_initial_input is True
    assert tracker.remaining_initial_input is None


def test_mark_input_as_sent_and_rewind_input_respects_remaining_initial_input() -> None:
    tracker = _ServerConversationTracker(conversation_id="conv2", previous_response_id=None)
    pending_1: TResponseInputItem = cast(TResponseInputItem, {"id": "p-1", "type": "message"})
    pending_2: TResponseInputItem = cast(TResponseInputItem, {"id": "p-2", "type": "message"})
    tracker.remaining_initial_input = [pending_1, pending_2]

    tracker.mark_input_as_sent(
        [pending_1, cast(TResponseInputItem, {"id": "p-2", "type": "message"})]
    )
    assert tracker.remaining_initial_input is None

    tracker.rewind_input([pending_1])
    assert tracker.remaining_initial_input == [pending_1]


def test_track_server_items_filters_remaining_initial_input_by_fingerprint() -> None:
    tracker = _ServerConversationTracker(conversation_id="conv3", previous_response_id=None)
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


def test_prepare_input_returns_json_serializable_dicts_not_pydantic_models() -> None:
    """Test that prepare_input converts Pydantic models to dicts via to_input_item().

    Issue: prepare_input was appending raw_item directly, which could be a Pydantic
    model (like ResponseOutputMessage), causing non-serializable items to be sent
    to the API. This test verifies that to_input_item() is called to convert models
    to dicts before appending.
    """
    tracker = _ServerConversationTracker(conversation_id="conv4", previous_response_id=None)

    # Create a RunItem with a Pydantic model as raw_item
    agent = Agent(name="TestAgent")
    pydantic_message = ResponseOutputMessage(
        id="msg-1",
        type="message",
        role="assistant",
        content=[ResponseOutputText(text="Hello", type="output_text", annotations=[], logprobs=[])],
        status="completed",
    )
    message_item = MessageOutputItem(agent=agent, raw_item=pydantic_message)

    original_input: list[TResponseInputItem] = []
    generated_items: list[RunItem] = [message_item]

    prepared = tracker.prepare_input(
        original_input=original_input,
        generated_items=generated_items,
        model_responses=None,
    )

    # Verify the result contains dicts, not Pydantic models
    assert len(prepared) == 1
    prepared_item = prepared[0]

    # Should be a dict, not a Pydantic model
    assert isinstance(prepared_item, dict), f"Expected dict, got {type(prepared_item)}"
    assert not isinstance(prepared_item, ResponseOutputMessage), "Should not be a Pydantic model"

    # Should be JSON-serializable
    try:
        json.dumps(prepared_item)
    except (TypeError, ValueError) as e:
        pytest.fail(f"Item is not JSON-serializable: {e}")

    # Verify the dict contains expected fields from the Pydantic model
    assert prepared_item.get("id") == "msg-1"
    assert prepared_item.get("type") == "message"
    assert prepared_item.get("role") == "assistant"


def test_prepare_input_tracks_sent_items_to_prevent_duplicates() -> None:
    """Test that prepare_input tracks sent items to prevent duplicates on subsequent calls.

    Issue: prepare_input appends items to input_items but never records them in sent_items,
    causing duplicates on subsequent calls. This test verifies that items are tracked in
    sent_items after being appended.

    Note: mark_input_as_sent() adds items to sent_items, but prepare_input() should also
    add items to sent_items when they're appended, so that subsequent calls to prepare_input
    don't re-send the same items.
    """
    tracker = _ServerConversationTracker(conversation_id="conv5", previous_response_id=None)
    agent = Agent(name="TestAgent")

    # Create a generated item with a dict raw_item that is NOT in server_items
    # and NOT in server_item_ids (so it won't be filtered out)
    raw_item_dict = {"type": "message", "content": "first message"}
    message_item = MessageOutputItem(
        agent=agent,
        raw_item=cast(Any, raw_item_dict),
    )

    original_input: list[TResponseInputItem] = []
    generated_items: list[RunItem] = [message_item]

    # First call to prepare_input
    prepared1 = tracker.prepare_input(
        original_input=original_input,
        generated_items=generated_items,
        model_responses=None,
    )

    # Verify the item was returned
    assert len(prepared1) == 1
    assert prepared1[0].get("content") == "first message"

    # Second call with the same items - should NOT return duplicates if items were tracked
    # But currently they're not tracked in prepare_input, so it WILL return duplicates
    # (this is the bug)
    prepared2 = tracker.prepare_input(
        original_input=original_input,
        generated_items=generated_items,
        model_responses=None,
    )

    # Currently fails: items are re-sent because they weren't tracked in sent_items by prepare_input
    assert len(prepared2) == 0, (
        "Items should not be re-sent on subsequent calls if they were tracked in sent_items. "
        "Currently prepare_input doesn't add items to sent_items, causing duplicates."
    )


def test_prepare_input_normalizes_tool_outputs_stripping_protocol_only_fields() -> None:
    """Test that prepare_input normalizes tool outputs by stripping protocol-only fields.

    Issue: prepare_input uses raw_item directly when it's a dict, bypassing the normalization
    that to_input_item() would do. ToolCallOutputItem.to_input_item() strips protocol-only fields
    like 'status', 'shell_output', 'provider_data' that the Responses API rejects. This test
    verifies that these fields are stripped when tool outputs are prepared for sending.

    Protocol-only fields that should be stripped:
    - 'status' (for function_call_output and shell_call_output)
    - 'shell_output' (for shell_call_output)
    - 'provider_data' (for shell_call_output)
    - 'name' (for function_call_result when converting to function_call_output)
    """
    tracker = _ServerConversationTracker(conversation_id="conv6", previous_response_id=None)
    agent = Agent(name="TestAgent")

    # Create a ToolCallOutputItem with a dict raw_item containing protocol-only fields
    # that should be stripped by to_input_item()
    tool_output_raw = {
        "type": "shell_call_output",
        "call_id": "call-123",
        "output": "command output",
        "status": "completed",  # Protocol-only field - should be stripped
        "shell_output": "raw shell output",  # Protocol-only field - should be stripped
        "provider_data": {"some": "data"},  # Protocol-only field - should be stripped
    }
    tool_output_item = ToolCallOutputItem(
        agent=agent,
        raw_item=tool_output_raw,
        output="command output",
    )

    original_input: list[TResponseInputItem] = []
    generated_items: list[RunItem] = [tool_output_item]

    prepared = tracker.prepare_input(
        original_input=original_input,
        generated_items=generated_items,
        model_responses=None,
    )

    # Verify the result contains normalized tool output without protocol-only fields
    assert len(prepared) == 1
    prepared_item = prepared[0]

    # Should be a dict
    assert isinstance(prepared_item, dict), f"Expected dict, got {type(prepared_item)}"

    # Verify protocol-only fields are stripped
    assert "status" not in prepared_item, (
        "Protocol-only field 'status' should be stripped from shell_call_output. "
        "Currently prepare_input uses raw_item directly when it's a dict, bypassing "
        "the normalization that to_input_item() would do."
    )
    assert "shell_output" not in prepared_item, (
        "Protocol-only field 'shell_output' should be stripped from shell_call_output. "
        "Currently prepare_input uses raw_item directly when it's a dict, bypassing "
        "the normalization that to_input_item() would do."
    )
    assert "provider_data" not in prepared_item, (
        "Protocol-only field 'provider_data' should be stripped from shell_call_output. "
        "Currently prepare_input uses raw_item directly when it's a dict, bypassing "
        "the normalization that to_input_item() would do."
    )

    # Verify required fields are still present
    assert prepared_item.get("type") == "shell_call_output"
    assert prepared_item.get("call_id") == "call-123"
    assert prepared_item.get("output") == "command output"
