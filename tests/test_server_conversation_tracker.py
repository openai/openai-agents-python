from typing import Any, cast

import pytest

from agents import Agent
from agents.items import ModelResponse, TResponseInputItem
from agents.lifecycle import RunHooks
from agents.models.fake_id import FAKE_RESPONSES_ID
from agents.result import RunResultStreaming
from agents.run_config import ModelInputData, RunConfig
from agents.run_context import RunContextWrapper
from agents.run_internal.oai_conversation import OpenAIServerConversationTracker
from agents.run_internal.run_loop import get_new_response, run_single_turn_streamed
from agents.run_internal.tool_use_tracker import AgentToolUseTracker
from agents.usage import Usage

from .fake_model import FakeModel
from .test_responses import get_text_message


class DummyRunItem:
    """Minimal stand-in for RunItem with the attributes used by OpenAIServerConversationTracker."""

    def __init__(self, raw_item: dict[str, Any], type: str = "message") -> None:
        self.raw_item = raw_item
        self.type = type


def test_prepare_input_filters_items_seen_by_server_and_tool_calls() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv", previous_response_id=None)

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

    tracker.hydrate_from_state(
        original_input=original_input,
        generated_items=cast(list[Any], generated_items),
        model_responses=[model_response],
        session_items=session_items,
    )

    prepared = tracker.prepare_input(
        original_input=original_input,
        generated_items=cast(list[Any], generated_items),
    )

    assert prepared == [new_raw_item]
    assert tracker.sent_initial_input is True
    assert tracker.remaining_initial_input is None


def test_mark_input_as_sent_and_rewind_input_respects_remaining_initial_input() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv2", previous_response_id=None)
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
    tracker = OpenAIServerConversationTracker(conversation_id="conv3", previous_response_id=None)
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


def test_prepare_input_does_not_skip_fake_response_ids() -> None:
    tracker = OpenAIServerConversationTracker(conversation_id="conv5", previous_response_id=None)

    model_response = object.__new__(ModelResponse)
    model_response.output = [cast(Any, {"id": FAKE_RESPONSES_ID, "type": "message"})]
    model_response.usage = Usage()
    model_response.response_id = "resp-3"

    tracker.track_server_items(model_response)

    raw_item = {"id": FAKE_RESPONSES_ID, "type": "message", "content": "hello"}
    generated_items = [DummyRunItem(raw_item)]

    prepared = tracker.prepare_input(
        original_input=[],
        generated_items=cast(list[Any], generated_items),
    )

    assert prepared == [raw_item]


@pytest.mark.asyncio
async def test_get_new_response_marks_filtered_input_as_sent() -> None:
    model = FakeModel()
    model.set_next_output([get_text_message("ok")])
    agent = Agent(name="test", model=model)
    tracker = OpenAIServerConversationTracker(conversation_id="conv4", previous_response_id=None)
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})
    tool_use_tracker = AgentToolUseTracker()

    item_1: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "first"})
    item_2: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "second"})

    def _filter_input(payload: Any) -> ModelInputData:
        return ModelInputData(
            input=[payload.model_data.input[0]],
            instructions=payload.model_data.instructions,
        )

    run_config = RunConfig(call_model_input_filter=_filter_input)

    await get_new_response(
        agent,
        None,
        [item_1, item_2],
        None,
        [],
        [],
        RunHooks(),
        context_wrapper,
        run_config,
        tool_use_tracker,
        tracker,
        None,
    )

    assert model.last_turn_args["input"] == [item_1]
    assert id(item_1) in tracker.sent_items
    assert id(item_2) not in tracker.sent_items


@pytest.mark.asyncio
async def test_run_single_turn_streamed_marks_filtered_input_as_sent() -> None:
    model = FakeModel()
    model.set_next_output([get_text_message("ok")])
    agent = Agent(name="test", model=model)
    tracker = OpenAIServerConversationTracker(conversation_id="conv6", previous_response_id=None)
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})
    tool_use_tracker = AgentToolUseTracker()

    item_1: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "first"})
    item_2: TResponseInputItem = cast(TResponseInputItem, {"role": "user", "content": "second"})

    def _filter_input(payload: Any) -> ModelInputData:
        return ModelInputData(
            input=[payload.model_data.input[0]],
            instructions=payload.model_data.instructions,
        )

    run_config = RunConfig(call_model_input_filter=_filter_input)

    streamed_result = RunResultStreaming(
        input=[item_1, item_2],
        new_items=[],
        raw_responses=[],
        final_output=None,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=context_wrapper,
        current_agent=agent,
        current_turn=0,
        max_turns=1,
        _current_agent_output_schema=None,
        trace=None,
        interruptions=[],
    )

    await run_single_turn_streamed(
        streamed_result,
        agent,
        RunHooks(),
        context_wrapper,
        run_config,
        should_run_agent_start_hooks=False,
        tool_use_tracker=tool_use_tracker,
        all_tools=[],
        server_conversation_tracker=tracker,
    )

    assert model.last_turn_args["input"] == [item_1]
    assert tracker.remaining_initial_input == [item_2]
