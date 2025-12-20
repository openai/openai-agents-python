import json
from typing import Any, cast

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents import Agent
from agents.items import MessageOutputItem, ModelResponse, RunItem, TResponseInputItem
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
