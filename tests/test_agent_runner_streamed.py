from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary
from typing_extensions import TypedDict

from agents import (
    Agent,
    GuardrailFunctionOutput,
    Handoff,
    HandoffInputData,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    OutputGuardrail,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    UserError,
    function_tool,
    handoff,
)
from agents.items import RunItem, ToolApprovalItem, TResponseInputItem
from agents.memory.openai_conversations_session import OpenAIConversationsSession
from agents.run import RunConfig
from agents.run_internal import run_loop
from agents.run_internal.run_loop import QueueCompleteSentinel
from agents.stream_events import AgentUpdatedStreamEvent, StreamEvent

from .fake_model import FakeModel
from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)
from .utils.hitl import (
    consume_stream,
    make_model_and_agent,
    queue_function_call_and_text,
    resume_streamed_after_first_approval,
)
from .utils.simple_session import SimpleListSession


@pytest.mark.asyncio
async def test_simple_first_run():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
    )
    model.set_next_output([get_text_message("first")])

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert result.input == "test"
    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert result.final_output == "first"
    assert len(result.raw_responses) == 1, "exactly one model response should be generated"
    assert result.raw_responses[0].output == [get_text_message("first")]
    assert result.last_agent == agent

    assert len(result.to_input_list()) == 2, "should have original input and generated item"

    model.set_next_output([get_text_message("second")])

    result = Runner.run_streamed(
        agent, input=[get_text_input_item("message"), get_text_input_item("another_message")]
    )
    async for _ in result.stream_events():
        pass

    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert result.final_output == "second"
    assert len(result.raw_responses) == 1, "exactly one model response should be generated"
    assert len(result.to_input_list()) == 3, "should have original input and generated item"


@pytest.mark.asyncio
async def test_subsequent_runs():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
    )
    model.set_next_output([get_text_message("third")])

    result = Runner.run_streamed(agent, input="test")
    async for _ in result.stream_events():
        pass

    assert result.input == "test"
    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert len(result.to_input_list()) == 2, "should have original input and generated item"

    model.set_next_output([get_text_message("fourth")])

    result = Runner.run_streamed(agent, input=result.to_input_list())
    async for _ in result.stream_events():
        pass

    assert len(result.input) == 2, f"should have previous input but got {result.input}"
    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert result.final_output == "fourth"
    assert len(result.raw_responses) == 1, "exactly one model response should be generated"
    assert result.raw_responses[0].output == [get_text_message("fourth")]
    assert result.last_agent == agent
    assert len(result.to_input_list()) == 3, "should have original input and generated items"


@pytest.mark.asyncio
async def test_tool_call_runs():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("foo", json.dumps({"a": "b"}))],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"
    assert len(result.raw_responses) == 2, (
        "should have two responses: the first which produces a tool call, and the second which"
        "handles the tool result"
    )

    assert len(result.to_input_list()) == 5, (
        "should have five inputs: the original input, the message, the tool call, the tool result "
        "and the done message"
    )


@pytest.mark.asyncio
async def test_streamed_run_again_persists_tool_items_to_session():
    model = FakeModel()
    call_id = "call-session-run-again"
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result")],
    )
    session = SimpleListSession()

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("foo", json.dumps({"a": "b"}), call_id=call_id)],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message", session=session)
    await consume_stream(result)

    saved_items = await session.get_items()
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == call_id
        for item in saved_items
    )
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == call_id
        for item in saved_items
    )


@pytest.mark.asyncio
async def test_handoffs():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )
    agent_2 = Agent(
        name="test",
        model=model,
    )
    agent_3 = Agent(
        name="test",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent_3, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"
    assert len(result.raw_responses) == 3, "should have three model responses"
    assert len(result.to_input_list()) == 7, (
        "should have 7 inputs: summary message, tool call, tool result, message, handoff, "
        "handoff result, and done message"
    )
    assert result.last_agent == agent_1, "should have handed off to agent_1"


class Foo(TypedDict):
    bar: str


@pytest.mark.asyncio
async def test_structured_output():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("bar", "bar_result")],
        output_type=Foo,
    )

    agent_2 = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "foo_result")],
        handoffs=[agent_1],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("foo", json.dumps({"bar": "baz"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: tool call with preamble message
            [
                get_text_message(json.dumps(Foo(bar="preamble"))),
                get_function_tool_call("bar", json.dumps({"bar": "baz"})),
            ],
            # Fourth turn: structured output
            [get_final_output_message(json.dumps(Foo(bar="baz")))],
        ]
    )

    result = Runner.run_streamed(
        agent_2,
        input=[
            get_text_input_item("user_message"),
            get_text_input_item("another_message"),
        ],
        run_config=RunConfig(nest_handoff_history=True),
    )
    async for _ in result.stream_events():
        pass

    assert result.final_output == Foo(bar="baz")
    assert len(result.raw_responses) == 4, "should have four model responses"
    assert len(result.to_input_list()) == 10, (
        "should have input: conversation summary, function call, function call result, message, "
        "handoff, handoff output, preamble message, tool call, tool call result, final output"
    )

    assert result.last_agent == agent_1, "should have handed off to agent_1"
    assert result.final_output == Foo(bar="baz"), "should have structured output"


def remove_new_items(handoff_input_data: HandoffInputData) -> HandoffInputData:
    return HandoffInputData(
        input_history=handoff_input_data.input_history,
        pre_handoff_items=(),
        new_items=(),
        run_context=handoff_input_data.run_context,
    )


@pytest.mark.asyncio
async def test_handoff_filters():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )
    agent_2 = Agent(
        name="test",
        model=model,
        handoffs=[
            handoff(
                agent=agent_1,
                input_filter=remove_new_items,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_text_message("2"), get_handoff_tool_call(agent_1)],
            [get_text_message("last")],
        ]
    )

    result = Runner.run_streamed(agent_2, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "last"
    assert len(result.raw_responses) == 2, "should have two model responses"
    assert len(result.to_input_list()) == 2, (
        "should only have 2 inputs: orig input and last message"
    )


@pytest.mark.asyncio
async def test_streamed_nested_handoff_filters_reasoning_items_from_model_input():
    model = FakeModel()
    delegate = Agent(
        name="delegate",
        model=model,
    )
    triage = Agent(
        name="triage",
        model=model,
        handoffs=[delegate],
    )

    model.add_multiple_turn_outputs(
        [
            [
                ResponseReasoningItem(
                    id="reasoning_1",
                    type="reasoning",
                    summary=[Summary(text="Thinking about a handoff.", type="summary_text")],
                ),
                get_handoff_tool_call(delegate),
            ],
            [get_text_message("done")],
        ]
    )

    captured_inputs: list[list[dict[str, Any]]] = []

    def capture_model_input(data):
        if isinstance(data.model_data.input, list):
            captured_inputs.append(
                [item for item in data.model_data.input if isinstance(item, dict)]
            )
        return data.model_data

    result = Runner.run_streamed(
        triage,
        input="user_message",
        run_config=RunConfig(
            nest_handoff_history=True,
            call_model_input_filter=capture_model_input,
        ),
    )
    await consume_stream(result)

    assert result.final_output == "done"
    assert len(captured_inputs) >= 2
    handoff_input = captured_inputs[1]
    handoff_input_types = [
        item["type"] for item in handoff_input if isinstance(item.get("type"), str)
    ]
    assert "reasoning" not in handoff_input_types


@pytest.mark.asyncio
async def test_async_input_filter_supported():
    # DO NOT rename this without updating pyproject.toml

    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    async def on_invoke_handoff(_ctx: RunContextWrapper[Any], _input: str) -> Agent[Any]:
        return agent_1

    async def async_input_filter(data: HandoffInputData) -> HandoffInputData:
        return data  # pragma: no cover

    agent_2 = Agent[None](
        name="test",
        model=model,
        handoffs=[
            Handoff(
                tool_name=Handoff.default_tool_name(agent_1),
                tool_description=Handoff.default_tool_description(agent_1),
                input_json_schema={},
                on_invoke_handoff=on_invoke_handoff,
                agent_name=agent_1.name,
                input_filter=async_input_filter,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_text_message("2"), get_handoff_tool_call(agent_1)],
            [get_text_message("last")],
        ]
    )

    result = Runner.run_streamed(agent_2, input="user_message")
    async for _ in result.stream_events():
        pass


@pytest.mark.asyncio
async def test_invalid_input_filter_fails():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    async def on_invoke_handoff(_ctx: RunContextWrapper[Any], _input: str) -> Agent[Any]:
        return agent_1

    def invalid_input_filter(data: HandoffInputData) -> HandoffInputData:
        # Purposely returning a string to simulate invalid output
        return "foo"  # type: ignore

    agent_2 = Agent[None](
        name="test",
        model=model,
        handoffs=[
            Handoff(
                tool_name=Handoff.default_tool_name(agent_1),
                tool_description=Handoff.default_tool_description(agent_1),
                input_json_schema={},
                on_invoke_handoff=on_invoke_handoff,
                agent_name=agent_1.name,
                input_filter=invalid_input_filter,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_text_message("2"), get_handoff_tool_call(agent_1)],
            [get_text_message("last")],
        ]
    )

    with pytest.raises(UserError):
        result = Runner.run_streamed(agent_2, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_non_callable_input_filter_causes_error():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    async def on_invoke_handoff(_ctx: RunContextWrapper[Any], _input: str) -> Agent[Any]:
        return agent_1

    agent_2 = Agent[None](
        name="test",
        model=model,
        handoffs=[
            Handoff(
                tool_name=Handoff.default_tool_name(agent_1),
                tool_description=Handoff.default_tool_description(agent_1),
                input_json_schema={},
                on_invoke_handoff=on_invoke_handoff,
                agent_name=agent_1.name,
                # Purposely ignoring the type error here to simulate invalid input
                input_filter="foo",  # type: ignore
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_text_message("2"), get_handoff_tool_call(agent_1)],
            [get_text_message("last")],
        ]
    )

    with pytest.raises(UserError):
        result = Runner.run_streamed(agent_2, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_handoff_on_input():
    call_output: str | None = None

    def on_input(_ctx: RunContextWrapper[Any], data: Foo) -> None:
        nonlocal call_output
        call_output = data["bar"]

    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    agent_2 = Agent(
        name="test",
        model=model,
        handoffs=[
            handoff(
                agent=agent_1,
                on_handoff=on_input,
                input_type=Foo,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("1"),
                get_text_message("2"),
                get_handoff_tool_call(agent_1, args=json.dumps(Foo(bar="test_input"))),
            ],
            [get_text_message("last")],
        ]
    )

    result = Runner.run_streamed(agent_2, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "last"

    assert call_output == "test_input", "should have called the handoff with the correct input"


@pytest.mark.asyncio
async def test_async_handoff_on_input():
    call_output: str | None = None

    async def on_input(_ctx: RunContextWrapper[Any], data: Foo) -> None:
        nonlocal call_output
        call_output = data["bar"]

    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
    )

    agent_2 = Agent(
        name="test",
        model=model,
        handoffs=[
            handoff(
                agent=agent_1,
                on_handoff=on_input,
                input_type=Foo,
            )
        ],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("1"),
                get_text_message("2"),
                get_handoff_tool_call(agent_1, args=json.dumps(Foo(bar="test_input"))),
            ],
            [get_text_message("last")],
        ]
    )

    result = Runner.run_streamed(agent_2, input="user_message")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "last"

    assert call_output == "test_input", "should have called the handoff with the correct input"


@pytest.mark.asyncio
async def test_input_guardrail_tripwire_triggered_causes_exception_streamed():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    agent = Agent(
        name="test",
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
        model=FakeModel(),
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_input_guardrail_streamed_does_not_save_assistant_message_to_session():
    async def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(0.01)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    session = SimpleListSession()

    model = FakeModel()
    model.set_next_output([get_text_message("should_not_be_saved")])

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message", session=session)
        async for _ in result.stream_events():
            pass

    items = await session.get_items()

    assert len(items) == 1
    first_item = cast(dict[str, Any], items[0])
    assert "role" in first_item
    assert first_item["role"] == "user"


@pytest.mark.asyncio
async def test_input_guardrail_streamed_persists_user_input_for_sequential_guardrail():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    session = SimpleListSession()

    model = FakeModel()
    model.set_next_output([get_text_message("should_not_be_saved")])

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[
            InputGuardrail(guardrail_function=guardrail_function, run_in_parallel=False)
        ],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message", session=session)
        async for _ in result.stream_events():
            pass

    items = await session.get_items()

    assert len(items) == 1
    first_item = cast(dict[str, Any], items[0])
    assert "role" in first_item
    assert first_item["role"] == "user"


@pytest.mark.asyncio
async def test_input_guardrail_streamed_persists_user_input_for_async_sequential_guardrail():
    async def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(0)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    session = SimpleListSession()

    model = FakeModel()
    model.set_next_output([get_text_message("should_not_be_saved")])

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[
            InputGuardrail(guardrail_function=guardrail_function, run_in_parallel=False)
        ],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message", session=session)
        async for _ in result.stream_events():
            pass

    items = await session.get_items()

    assert len(items) == 1
    first_item = cast(dict[str, Any], items[0])
    assert "role" in first_item
    assert first_item["role"] == "user"


@pytest.mark.asyncio
async def test_stream_input_persistence_strips_ids_for_openai_conversation_session():
    class DummyOpenAIConversationsSession(OpenAIConversationsSession):
        def __init__(self) -> None:
            self.saved: list[list[TResponseInputItem]] = []

        async def _get_session_id(self) -> str:
            return "conv_test"

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            for item in items:
                if isinstance(item, dict):
                    assert "id" not in item, "IDs should be stripped before saving"
                    assert "provider_data" not in item, (
                        "provider_data should be stripped before saving"
                    )
            self.saved.append(items)

        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            return []

        async def pop_item(self) -> TResponseInputItem | None:
            return None

        async def clear_session(self) -> None:
            return None

    session = DummyOpenAIConversationsSession()

    model = FakeModel()
    model.set_next_output([get_text_message("ok")])

    agent = Agent(
        name="test",
        model=model,
    )

    run_config = RunConfig(session_input_callback=lambda existing, new: existing + new)

    input_items = [
        cast(
            TResponseInputItem,
            {
                "id": "message-1",
                "type": "message",
                "role": "user",
                "content": "hello",
                "provider_data": {"model": "litellm/test"},
            },
        )
    ]

    result = Runner.run_streamed(agent, input=input_items, session=session, run_config=run_config)
    async for _ in result.stream_events():
        pass

    assert session.saved, "input items should be persisted via save_result_to_session"
    assert len(session.saved[0]) == 1
    saved_item = session.saved[0][0]
    assert isinstance(saved_item, dict)
    assert "id" not in saved_item, "saved input items should not include IDs"


@pytest.mark.asyncio
async def test_stream_input_persistence_saves_only_new_turn_input(monkeypatch: pytest.MonkeyPatch):
    session = SimpleListSession()
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_text_message("first")],
            [get_text_message("second")],
        ]
    )
    agent = Agent(name="test", model=model)

    from agents.run_internal import session_persistence as sp

    real_save_result = sp.save_result_to_session
    input_saves: list[list[TResponseInputItem]] = []

    async def save_wrapper(
        sess: Any,
        original_input: Any,
        new_items: list[RunItem],
        run_state: Any = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(original_input, list) and original_input:
            input_saves.append(list(original_input))
        await real_save_result(sess, original_input, new_items, run_state, **kwargs)

    monkeypatch.setattr(
        "agents.run_internal.session_persistence.save_result_to_session", save_wrapper
    )
    monkeypatch.setattr("agents.run_internal.run_loop.save_result_to_session", save_wrapper)

    run_config = RunConfig(session_input_callback=lambda existing, new: existing + new)

    first = Runner.run_streamed(
        agent, input=[get_text_input_item("hello")], session=session, run_config=run_config
    )
    async for _ in first.stream_events():
        pass

    second = Runner.run_streamed(
        agent, input=[get_text_input_item("next")], session=session, run_config=run_config
    )
    async for _ in second.stream_events():
        pass

    assert len(input_saves) == 2, "each turn should persist only the turn input once"
    assert all(len(saved) == 1 for saved in input_saves), (
        "each persisted input should contain only the new turn items"
    )
    first_saved = input_saves[0][0]
    second_saved = input_saves[1][0]
    assert isinstance(first_saved, dict) and first_saved.get("content") == "hello"
    assert isinstance(second_saved, dict) and second_saved.get("content") == "next"


@pytest.mark.asyncio
async def test_slow_input_guardrail_still_raises_exception_streamed():
    async def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        # Simulate a slow guardrail that completes after model streaming ends.
        await asyncio.sleep(0.05)
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    model = FakeModel()
    # Ensure the model finishes streaming quickly.
    model.set_next_output([get_text_message("ok")])

    agent = Agent(
        name="test",
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
        model=model,
    )

    # Even though the guardrail is slower than the model stream, the exception should still raise.
    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_output_guardrail_tripwire_triggered_causes_exception_streamed():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    model = FakeModel(initial_output=[get_text_message("first_test")])

    agent = Agent(
        name="test",
        output_guardrails=[OutputGuardrail(guardrail_function=guardrail_function)],
        model=model,
    )

    with pytest.raises(OutputGuardrailTripwireTriggered):
        result = Runner.run_streamed(agent, input="user_message")
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_run_input_guardrail_tripwire_triggered_causes_exception_streamed():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    agent = Agent(
        name="test",
        model=FakeModel(),
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        result = Runner.run_streamed(
            agent,
            input="user_message",
            run_config=RunConfig(
                input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)]
            ),
        )
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_run_output_guardrail_tripwire_triggered_causes_exception_streamed():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    model = FakeModel(initial_output=[get_text_message("first_test")])

    agent = Agent(
        name="test",
        model=model,
    )

    with pytest.raises(OutputGuardrailTripwireTriggered):
        result = Runner.run_streamed(
            agent,
            input="user_message",
            run_config=RunConfig(
                output_guardrails=[OutputGuardrail(guardrail_function=guardrail_function)]
            ),
        )
        async for _ in result.stream_events():
            pass


@pytest.mark.asyncio
async def test_streaming_events():
    model = FakeModel()
    agent_1 = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("bar", "bar_result")],
        output_type=Foo,
    )

    agent_2 = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "foo_result")],
        handoffs=[agent_1],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("foo", json.dumps({"bar": "baz"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: tool call
            [get_function_tool_call("bar", json.dumps({"bar": "baz"}))],
            # Fourth turn: structured output
            [get_final_output_message(json.dumps(Foo(bar="baz")))],
        ]
    )

    # event_type: (count, event)
    event_counts: dict[str, int] = {}
    item_data: list[RunItem] = []
    agent_data: list[AgentUpdatedStreamEvent] = []

    result = Runner.run_streamed(
        agent_2,
        input=[
            get_text_input_item("user_message"),
            get_text_input_item("another_message"),
        ],
        run_config=RunConfig(nest_handoff_history=True),
    )
    async for event in result.stream_events():
        event_counts[event.type] = event_counts.get(event.type, 0) + 1
        if event.type == "run_item_stream_event":
            item_data.append(event.item)
        elif event.type == "agent_updated_stream_event":
            agent_data.append(event)

    assert result.final_output == Foo(bar="baz")
    assert len(result.raw_responses) == 4, "should have four model responses"
    assert len(result.to_input_list()) == 9, (
        "should have input: conversation summary, function call, function call result, message, "
        "handoff, handoff output, tool call, tool call result, final output"
    )

    assert result.last_agent == agent_1, "should have handed off to agent_1"
    assert result.final_output == Foo(bar="baz"), "should have structured output"

    # Now lets check the events

    expected_item_type_map = {
        # 3 tool_call_item events:
        #   1. get_function_tool_call("foo", ...)
        #   2. get_handoff_tool_call(agent_1) because handoffs are implemented via tool calls too
        #   3. get_function_tool_call("bar", ...)
        "tool_call": 3,
        # Only 2 outputs, handoff tool call doesn't have corresponding tool_call_output event
        "tool_call_output": 2,
        "message": 2,  # get_text_message("a_message") + get_final_output_message(...)
        "handoff": 1,  # get_handoff_tool_call(agent_1)
        "handoff_output": 1,  # handoff_output_item
    }

    total_expected_item_count = sum(expected_item_type_map.values())

    assert event_counts["run_item_stream_event"] == total_expected_item_count, (
        f"Expected {total_expected_item_count} events, got {event_counts['run_item_stream_event']}"
        f"Expected events were: {expected_item_type_map}, got {event_counts}"
    )

    assert len(item_data) == total_expected_item_count, (
        f"should have {total_expected_item_count} run items"
    )
    assert len(agent_data) == 2, "should have 2 agent updated events"
    assert agent_data[0].new_agent == agent_2, "should have started with agent_2"
    assert agent_data[1].new_agent == agent_1, "should have handed off to agent_1"


@pytest.mark.asyncio
async def test_dynamic_tool_addition_run_streamed() -> None:
    model = FakeModel()

    executed: dict[str, bool] = {"called": False}

    agent = Agent(name="test", model=model, tool_use_behavior="run_llm_again")

    @function_tool(name_override="tool2")
    def tool2() -> str:
        executed["called"] = True
        return "result2"

    @function_tool(name_override="add_tool")
    async def add_tool() -> str:
        agent.tools.append(tool2)
        return "added"

    agent.tools.append(add_tool)

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("add_tool", json.dumps({}))],
            [get_function_tool_call("tool2", json.dumps({}))],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="start")
    async for _ in result.stream_events():
        pass

    assert executed["called"] is True
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_stream_step_items_to_queue_handles_tool_approval_item():
    """Test that stream_step_items_to_queue handles ToolApprovalItem."""
    _, agent = make_model_and_agent(name="test")
    tool_call = get_function_tool_call("test_tool", "{}")
    assert isinstance(tool_call, ResponseFunctionToolCall)
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)

    queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel] = asyncio.Queue()

    # ToolApprovalItem should not be streamed
    run_loop.stream_step_items_to_queue([approval_item], queue)

    # Queue should be empty since ToolApprovalItem is not streamed
    assert queue.empty()


@pytest.mark.asyncio
async def test_streaming_hitl_resume_with_approved_tools():
    """Test resuming streaming run from RunState with approved tools executes them."""
    tool_called = False

    async def test_tool() -> str:
        nonlocal tool_called
        tool_called = True
        return "tool_result"

    # Create a tool that requires approval
    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])

    # First run - tool call that requires approval
    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({})),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool")
    await consume_stream(first)

    # Resume from state - should execute approved tool
    result2 = await resume_streamed_after_first_approval(agent, first)

    # Tool should have been called
    assert tool_called is True
    assert result2.final_output == "done"


@pytest.mark.asyncio
async def test_streaming_resume_with_session_does_not_duplicate_items():
    """Ensure session persistence does not duplicate tool items after streaming resume."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])
    session = SimpleListSession()

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({}), call_id="call-resume"),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool", session=session)
    await consume_stream(first)
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(agent, state, session=session)
    await consume_stream(resumed)
    assert resumed.final_output == "done"

    saved_items = await session.get_items()
    call_count = sum(
        1
        for item in saved_items
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == "call-resume"
    )
    output_count = sum(
        1
        for item in saved_items
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == "call-resume"
    )

    assert call_count == 1
    assert output_count == 1


@pytest.mark.asyncio
async def test_streaming_resume_preserves_filtered_model_input_after_handoff():
    model = FakeModel()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        return "ok"

    delegate = Agent(
        name="delegate",
        model=model,
        tools=[approval_tool],
    )
    triage = Agent(
        name="triage",
        model=model,
        handoffs=[delegate],
        tools=[get_function_tool("some_function", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "some_function", json.dumps({"a": "b"}), call_id="triage-call"
                )
            ],
            [get_text_message("a_message"), get_handoff_tool_call(delegate)],
            [get_function_tool_call("approval_tool", json.dumps({}), call_id="delegate-call")],
            [get_text_message("done")],
        ]
    )

    model_input_call_ids: list[set[str]] = []
    model_input_output_call_ids: list[set[str]] = []

    def capture_model_input(data):
        call_ids: set[str] = set()
        output_call_ids: set[str] = set()
        for item in data.model_data.input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            if item_type == "function_call":
                call_ids.add(call_id)
            elif item_type == "function_call_output":
                output_call_ids.add(call_id)
        model_input_call_ids.append(call_ids)
        model_input_output_call_ids.append(output_call_ids)
        return data.model_data

    run_config = RunConfig(
        nest_handoff_history=True,
        call_model_input_filter=capture_model_input,
    )

    first = Runner.run_streamed(triage, input="user_message", run_config=run_config)
    await consume_stream(first)
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(triage, state, run_config=run_config)
    await consume_stream(resumed)

    last_call_ids = model_input_call_ids[-1]
    last_output_call_ids = model_input_output_call_ids[-1]
    assert "triage-call" not in last_call_ids
    assert "triage-call" not in last_output_call_ids
    assert "delegate-call" in last_call_ids
    assert "delegate-call" in last_output_call_ids
    assert resumed.final_output == "done"


@pytest.mark.asyncio
async def test_streaming_resume_persists_tool_outputs_on_run_again():
    """Approved tool outputs should be persisted before streaming resumes the next turn."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])
    session = SimpleListSession()

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({}), call_id="call-resume"),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool", session=session)
    await consume_stream(first)

    assert first.interruptions
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(agent, state, session=session)
    await consume_stream(resumed)

    saved_items = await session.get_items()
    assert any(
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == "call-resume"
        for item in saved_items
    ), "approved tool outputs should be persisted on resume"


@pytest.mark.asyncio
async def test_streaming_resume_carries_persisted_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure resumed streaming preserves the persisted count for session saves."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])
    session = SimpleListSession()

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({}), call_id="call-resume"),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool", session=session)
    await consume_stream(first)
    assert first.interruptions

    persisted_count = first._current_turn_persisted_item_count
    assert persisted_count > 0

    state = first.to_state()
    state.approve(first.interruptions[0])

    observed_counts: list[int] = []
    run_loop_any = cast(Any, run_loop)
    real_save_resumed = run_loop_any.save_resumed_turn_items

    async def save_wrapper(
        *,
        session: Any,
        items: list[RunItem],
        persisted_count: int,
        response_id: str | None,
        store: bool | None = None,
    ) -> int:
        observed_counts.append(persisted_count)
        result = await real_save_resumed(
            session=session,
            items=items,
            persisted_count=persisted_count,
            response_id=response_id,
            store=store,
        )
        return int(result)

    monkeypatch.setattr(run_loop_any, "save_resumed_turn_items", save_wrapper)

    resumed = Runner.run_streamed(agent, state, session=session)
    await consume_stream(resumed)

    assert observed_counts, "expected resumed save to capture persisted count"
    assert all(count == persisted_count for count in observed_counts)


@pytest.mark.asyncio
async def test_streaming_hitl_resume_enforces_max_turns():
    """Test that streamed resumes advance turn counts for max_turns enforcement."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({})),
        followup=[get_text_message("done")],
    )

    first = Runner.run_streamed(agent, input="Use test_tool", max_turns=1)
    await consume_stream(first)

    assert first.interruptions
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(agent, state)
    with pytest.raises(MaxTurnsExceeded):
        async for _ in resumed.stream_events():
            pass


@pytest.mark.asyncio
async def test_streaming_max_turns_emits_pending_tool_output_events() -> None:
    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool")
    model, agent = make_model_and_agent(name="test", tools=[tool])

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({})),
        followup=[get_text_message("done")],
    )

    result = Runner.run_streamed(agent, input="Use test_tool", max_turns=1)
    streamed_item_types: list[str] = []

    with pytest.raises(MaxTurnsExceeded):
        async for event in result.stream_events():
            if event.type == "run_item_stream_event":
                streamed_item_types.append(event.item.type)

    assert "tool_call_item" in streamed_item_types
    assert "tool_call_output_item" in streamed_item_types


@pytest.mark.asyncio
async def test_streaming_non_max_turns_exception_does_not_emit_queued_events() -> None:
    model, agent = make_model_and_agent(name="test")
    model.set_next_output([get_text_message("done")])

    result = Runner.run_streamed(agent, input="hello")
    result.cancel()
    await asyncio.sleep(0)

    while not result._event_queue.empty():
        result._event_queue.get_nowait()
        result._event_queue.task_done()

    result._stored_exception = RuntimeError("guardrail-triggered")
    result._event_queue.put_nowait(AgentUpdatedStreamEvent(new_agent=agent))

    streamed_events: list[StreamEvent] = []
    with pytest.raises(RuntimeError, match="guardrail-triggered"):
        async for event in result.stream_events():
            streamed_events.append(event)

    assert streamed_events == []


@pytest.mark.asyncio
async def test_streaming_hitl_server_conversation_tracker_priming():
    """Test that resuming streaming run from RunState primes server conversation tracker."""
    model, agent = make_model_and_agent(name="test")

    # First run with conversation_id
    model.set_next_output([get_text_message("First response")])
    result1 = Runner.run_streamed(
        agent, input="test", conversation_id="conv123", previous_response_id="resp123"
    )
    await consume_stream(result1)

    # Create state from result
    state = result1.to_state()

    # Resume with same conversation_id - should not duplicate messages
    model.set_next_output([get_text_message("Second response")])
    result2 = Runner.run_streamed(
        agent, state, conversation_id="conv123", previous_response_id="resp123"
    )
    await consume_stream(result2)

    # Should complete successfully without message duplication
    assert result2.final_output == "Second response"
    assert len(result2.new_items) >= 1
