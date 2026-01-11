from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from openai import BadRequestError
from openai.types.responses import ResponseFunctionToolCall
from typing_extensions import TypedDict

from agents import (
    Agent,
    GuardrailFunctionOutput,
    Handoff,
    HandoffInputData,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    ModelBehaviorError,
    ModelSettings,
    OutputGuardrail,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    RunContextWrapper,
    Runner,
    SQLiteSession,
    UserError,
    handoff,
)
from agents.agent import ToolsToFinalOutputResult
from agents.computer import Computer
from agents.items import (
    ModelResponse,
    RunItem,
    ToolApprovalItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from agents.lifecycle import RunHooks
from agents.run import AgentRunner, get_default_agent_runner, set_default_agent_runner
from agents.run_config import _default_trace_include_sensitive_data
from agents.run_internal.items import (
    drop_orphan_function_calls,
    ensure_input_item_format,
    normalize_input_items_for_api,
)
from agents.run_internal.oai_conversation import OpenAIServerConversationTracker
from agents.run_internal.run_loop import get_new_response
from agents.run_internal.run_steps import NextStepFinalOutput, SingleStepResult
from agents.run_internal.session_persistence import (
    prepare_input_with_session,
    rewind_session_items,
    save_result_to_session,
)
from agents.run_internal.tool_execution import execute_approved_tools
from agents.run_internal.tool_use_tracker import AgentToolUseTracker
from agents.run_state import RunState
from agents.tool import ComputerTool, FunctionToolResult, function_tool
from agents.usage import Usage

from .fake_model import FakeModel
from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)
from .utils.factories import make_run_state
from .utils.hitl import make_context_wrapper, make_model_and_agent
from .utils.simple_session import CountingSession, IdStrippingSession, SimpleListSession


class _DummyRunItem:
    def __init__(self, payload: dict[str, Any], item_type: str = "tool_call_output_item"):
        self._payload = payload
        self.type = item_type

    def to_input_item(self) -> dict[str, Any]:
        return self._payload


async def run_execute_approved_tools(
    agent: Agent[Any],
    approval_item: ToolApprovalItem,
    *,
    approve: bool | None,
) -> list[RunItem]:
    """Execute approved tools with a consistent setup."""

    context_wrapper: RunContextWrapper[Any] = make_context_wrapper()
    state = make_run_state(
        agent,
        context=context_wrapper,
        original_input="test",
        max_turns=1,
    )

    if approve is True:
        state.approve(approval_item)
    elif approve is False:
        state.reject(approval_item)

    generated_items: list[RunItem] = []

    all_tools = await agent.get_all_tools(context_wrapper)
    await execute_approved_tools(
        agent=agent,
        interruptions=[approval_item],
        context_wrapper=context_wrapper,
        generated_items=generated_items,
        run_config=RunConfig(),
        hooks=RunHooks(),
        all_tools=all_tools,
    )

    return generated_items


def test_set_default_agent_runner_roundtrip():
    runner = AgentRunner()
    set_default_agent_runner(runner)
    assert get_default_agent_runner() is runner

    # Reset to ensure other tests are unaffected.
    set_default_agent_runner(None)
    assert isinstance(get_default_agent_runner(), AgentRunner)


def test_default_trace_include_sensitive_data_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "false")
    assert _default_trace_include_sensitive_data() is False

    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "TRUE")
    assert _default_trace_include_sensitive_data() is True


def testdrop_orphan_function_calls_removes_orphans():
    items: list[TResponseInputItem] = [
        cast(
            TResponseInputItem,
            {
                "type": "function_call",
                "call_id": "call_orphan",
                "name": "tool_one",
                "arguments": "{}",
            },
        ),
        cast(TResponseInputItem, {"type": "message", "role": "user", "content": "hello"}),
        cast(
            TResponseInputItem,
            {
                "type": "function_call",
                "call_id": "call_keep",
                "name": "tool_keep",
                "arguments": "{}",
            },
        ),
        cast(
            TResponseInputItem,
            {"type": "function_call_output", "call_id": "call_keep", "output": "done"},
        ),
    ]

    filtered = drop_orphan_function_calls(items)
    assert len(filtered) == 3
    for entry in filtered:
        if isinstance(entry, dict):
            assert entry.get("call_id") != "call_orphan"


def testnormalize_input_items_for_api_preserves_provider_data():
    items: list[TResponseInputItem] = [
        cast(
            TResponseInputItem,
            {
                "type": "function_call_output",
                "call_id": "call_norm",
                "status": "completed",
                "output": "out",
                "provider_data": {"trace": "keep"},
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "message",
                "role": "user",
                "content": "hi",
                "provider_data": {"trace": "remove"},
            },
        ),
    ]

    normalized = normalize_input_items_for_api(items)
    first = cast(dict[str, Any], normalized[0])
    second = cast(dict[str, Any], normalized[1])

    assert first["type"] == "function_call_output"
    assert first["call_id"] == "call_norm"
    assert first["provider_data"] == {"trace": "keep"}
    assert second["role"] == "user"
    assert second["provider_data"] == {"trace": "remove"}


def test_server_conversation_tracker_tracks_previous_response_id():
    tracker = OpenAIServerConversationTracker(conversation_id=None, previous_response_id="resp_a")
    response = ModelResponse(
        output=[get_text_message("hello")],
        usage=Usage(),
        response_id="resp_b",
    )
    tracker.track_server_items(response)

    assert tracker.previous_response_id == "resp_b"
    assert len(tracker.server_items) == 1


def _as_message(item: Any) -> dict[str, Any]:
    assert isinstance(item, dict)
    role = item.get("role")
    assert isinstance(role, str)
    assert role in {"assistant", "user", "system", "developer"}
    return cast(dict[str, Any], item)


@pytest.mark.asyncio
async def test_simple_first_run():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
    )
    model.set_next_output([get_text_message("first")])

    result = await Runner.run(agent, input="test")
    assert result.input == "test"
    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert result.final_output == "first"
    assert len(result.raw_responses) == 1, "exactly one model response should be generated"
    assert result.raw_responses[0].output == [get_text_message("first")]
    assert result.last_agent == agent

    assert len(result.to_input_list()) == 2, "should have original input and generated item"

    model.set_next_output([get_text_message("second")])

    result = await Runner.run(
        agent, input=[get_text_input_item("message"), get_text_input_item("another_message")]
    )
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

    result = await Runner.run(agent, input="test")
    assert result.input == "test"
    assert len(result.new_items) == 1, "exactly one item should be generated"
    assert len(result.to_input_list()) == 2, "should have original input and generated item"

    model.set_next_output([get_text_message("fourth")])

    result = await Runner.run(agent, input=result.to_input_list())
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

    result = await Runner.run(agent, input="user_message")

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

    result = await Runner.run(agent_3, input="user_message")

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

    result = await Runner.run(
        agent_2,
        input=[
            get_text_input_item("user_message"),
            get_text_input_item("another_message"),
        ],
    )

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

    result = await Runner.run(agent_2, input="user_message")

    assert result.final_output == "last"
    assert len(result.raw_responses) == 2, "should have two model responses"
    assert len(result.to_input_list()) == 2, (
        "should only have 2 inputs: orig input and last message"
    )


@pytest.mark.asyncio
async def test_default_handoff_history_nested_and_filters_respected():
    model = FakeModel()
    agent_1 = Agent(
        name="delegate",
        model=model,
    )
    agent_2 = Agent(
        name="triage",
        model=model,
        handoffs=[agent_1],
    )

    model.add_multiple_turn_outputs(
        [
            [get_text_message("triage summary"), get_handoff_tool_call(agent_1)],
            [get_text_message("resolution")],
        ]
    )

    result = await Runner.run(agent_2, input="user_message")

    assert isinstance(result.input, list)
    assert len(result.input) == 1
    summary = _as_message(result.input[0])
    assert summary["role"] == "assistant"
    summary_content = summary["content"]
    assert isinstance(summary_content, str)
    assert "<CONVERSATION HISTORY>" in summary_content
    assert "triage summary" in summary_content
    assert "user_message" in summary_content

    passthrough_model = FakeModel()
    delegate = Agent(name="delegate", model=passthrough_model)

    def passthrough_filter(data: HandoffInputData) -> HandoffInputData:
        return data

    triage_with_filter = Agent(
        name="triage",
        model=passthrough_model,
        handoffs=[handoff(delegate, input_filter=passthrough_filter)],
    )

    passthrough_model.add_multiple_turn_outputs(
        [
            [get_text_message("triage summary"), get_handoff_tool_call(delegate)],
            [get_text_message("resolution")],
        ]
    )

    filtered_result = await Runner.run(triage_with_filter, input="user_message")

    assert isinstance(filtered_result.input, str)
    assert filtered_result.input == "user_message"


@pytest.mark.asyncio
async def test_default_handoff_history_accumulates_across_multiple_handoffs():
    triage_model = FakeModel()
    delegate_model = FakeModel()
    closer_model = FakeModel()

    closer = Agent(name="closer", model=closer_model)
    delegate = Agent(name="delegate", model=delegate_model, handoffs=[closer])
    triage = Agent(name="triage", model=triage_model, handoffs=[delegate])

    triage_model.add_multiple_turn_outputs(
        [[get_text_message("triage summary"), get_handoff_tool_call(delegate)]]
    )
    delegate_model.add_multiple_turn_outputs(
        [[get_text_message("delegate update"), get_handoff_tool_call(closer)]]
    )
    closer_model.add_multiple_turn_outputs([[get_text_message("resolution")]])

    result = await Runner.run(triage, input="user_question")

    assert result.final_output == "resolution"
    assert closer_model.first_turn_args is not None
    closer_input = closer_model.first_turn_args["input"]
    assert isinstance(closer_input, list)
    summary = _as_message(closer_input[0])
    assert summary["role"] == "assistant"
    summary_content = summary["content"]
    assert isinstance(summary_content, str)
    assert summary_content.count("<CONVERSATION HISTORY>") == 1
    assert "triage summary" in summary_content
    assert "delegate update" in summary_content
    assert "user_question" in summary_content


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

    result = await Runner.run(agent_2, input="user_message")
    assert result.final_output == "last"


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
        await Runner.run(agent_2, input="user_message")


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
        await Runner.run(agent_2, input="user_message")


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

    result = await Runner.run(agent_2, input="user_message")

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

    result = await Runner.run(agent_2, input="user_message")

    assert result.final_output == "last"

    assert call_output == "test_input", "should have called the handoff with the correct input"


@pytest.mark.asyncio
async def test_wrong_params_on_input_causes_error():
    agent_1 = Agent(
        name="test",
    )

    def _on_handoff_too_many_params(ctx: RunContextWrapper[Any], foo: Foo, bar: str) -> None:
        pass

    with pytest.raises(UserError):
        handoff(
            agent_1,
            input_type=Foo,
            # Purposely ignoring the type error here to simulate invalid input
            on_handoff=_on_handoff_too_many_params,  # type: ignore
        )

    def on_handoff_too_few_params(ctx: RunContextWrapper[Any]) -> None:
        pass

    with pytest.raises(UserError):
        handoff(
            agent_1,
            input_type=Foo,
            # Purposely ignoring the type error here to simulate invalid input
            on_handoff=on_handoff_too_few_params,  # type: ignore
        )


@pytest.mark.asyncio
async def test_invalid_handoff_input_json_causes_error():
    agent = Agent(name="test")
    h = handoff(agent, input_type=Foo, on_handoff=lambda _ctx, _input: None)

    with pytest.raises(ModelBehaviorError):
        await h.on_invoke_handoff(
            RunContextWrapper(None),
            # Purposely ignoring the type error here to simulate invalid input
            None,  # type: ignore
        )

    with pytest.raises(ModelBehaviorError):
        await h.on_invoke_handoff(RunContextWrapper(None), "invalid")


@pytest.mark.asyncio
async def test_input_guardrail_tripwire_triggered_causes_exception():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    agent = Agent(
        name="test", input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)]
    )
    model = FakeModel()
    model.set_next_output([get_text_message("user_message")])

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, input="user_message")


@pytest.mark.asyncio
async def test_input_guardrail_tripwire_does_not_save_assistant_message_to_session():
    async def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        # Delay to ensure the agent has time to produce output before the guardrail finishes.
        await asyncio.sleep(0.01)
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    session = SimpleListSession()

    model = FakeModel()
    model.set_next_output([get_text_message("should_not_be_saved")])

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
    )

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, input="user_message", session=session)

    items = await session.get_items()

    assert len(items) == 1
    first_item = cast(dict[str, Any], items[0])
    assert "role" in first_item
    assert first_item["role"] == "user"


@pytest.mark.asyncio
async def test_prepare_input_with_session_keeps_function_call_outputs():
    history_item = cast(
        TResponseInputItem,
        {
            "type": "function_call_output",
            "call_id": "call_prepare",
            "output": "ok",
        },
    )
    session = SimpleListSession(history=[history_item])

    prepared_input, session_items = await prepare_input_with_session("hello", session, None)

    assert isinstance(prepared_input, list)
    assert len(session_items) == 1
    assert cast(dict[str, Any], session_items[0]).get("role") == "user"
    first_item = cast(dict[str, Any], prepared_input[0])
    last_item = cast(dict[str, Any], prepared_input[-1])
    assert first_item["type"] == "function_call_output"
    assert last_item["role"] == "user"
    assert last_item["content"] == "hello"


def test_ensure_api_input_item_handles_model_dump_objects():
    class _ModelDumpItem:
        def model_dump(self, exclude_unset: bool = True) -> dict[str, Any]:
            return {
                "type": "function_call_output",
                "call_id": "call_model_dump",
                "output": "dumped",
            }

    dummy_item: Any = _ModelDumpItem()
    converted = ensure_input_item_format(dummy_item)
    assert converted["type"] == "function_call_output"
    assert converted["output"] == "dumped"


def test_ensure_api_input_item_preserves_object_output():
    payload = cast(
        TResponseInputItem,
        {
            "type": "function_call_output",
            "call_id": "call_object",
            "output": {"complex": "value"},
        },
    )

    converted = ensure_input_item_format(payload)
    assert converted["type"] == "function_call_output"
    assert isinstance(converted["output"], dict)
    assert converted["output"] == {"complex": "value"}


@pytest.mark.asyncio
async def test_prepare_input_with_session_uses_sync_callback():
    history_item = cast(TResponseInputItem, {"role": "user", "content": "hi"})
    session = SimpleListSession(history=[history_item])

    def callback(
        history: list[TResponseInputItem], new_input: list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        first = cast(dict[str, Any], history[0])
        assert first["role"] == "user"
        return history + new_input

    prepared, session_items = await prepare_input_with_session("second", session, callback)
    assert len(prepared) == 2
    last_item = cast(dict[str, Any], prepared[-1])
    assert last_item["role"] == "user"
    assert last_item.get("content") == "second"
    # session_items should contain only the new turn input
    assert len(session_items) == 1
    assert cast(dict[str, Any], session_items[0]).get("role") == "user"


@pytest.mark.asyncio
async def test_prepare_input_with_session_awaits_async_callback():
    history_item = cast(TResponseInputItem, {"role": "user", "content": "initial"})
    session = SimpleListSession(history=[history_item])

    async def callback(
        history: list[TResponseInputItem], new_input: list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        await asyncio.sleep(0)
        return history + new_input

    prepared, session_items = await prepare_input_with_session("later", session, callback)
    assert len(prepared) == 2
    first_item = cast(dict[str, Any], prepared[0])
    assert first_item["role"] == "user"
    assert first_item.get("content") == "initial"
    assert len(session_items) == 1
    assert cast(dict[str, Any], session_items[0]).get("role") == "user"


@pytest.mark.asyncio
async def test_conversation_lock_rewind_skips_when_no_snapshot() -> None:
    history_item = cast(TResponseInputItem, {"id": "old", "type": "message"})
    new_item = cast(TResponseInputItem, {"id": "new", "type": "message"})
    session = CountingSession(history=[history_item])

    request = httpx.Request("POST", "https://example.com")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"code": "conversation_locked", "message": "locked"}},
    )
    locked_error = BadRequestError(
        "locked",
        response=response,
        body={"error": {"code": "conversation_locked"}},
    )
    locked_error.code = "conversation_locked"

    model = FakeModel()
    model.add_multiple_turn_outputs([locked_error, [get_text_message("ok")]])
    agent = Agent(name="test", model=model)

    result = await get_new_response(
        agent=agent,
        system_prompt=None,
        input=[history_item, new_item],
        output_schema=None,
        all_tools=[],
        handoffs=[],
        hooks=RunHooks(),
        context_wrapper=RunContextWrapper(context={}),
        run_config=RunConfig(),
        tool_use_tracker=AgentToolUseTracker(),
        server_conversation_tracker=None,
        prompt_config=None,
        session=session,
        session_items_to_rewind=[],
    )

    assert isinstance(result, ModelResponse)
    assert session.pop_calls == 0


@pytest.mark.asyncio
async def test_save_result_to_session_preserves_function_outputs():
    session = SimpleListSession()
    original_item = cast(
        TResponseInputItem,
        {
            "type": "function_call_output",
            "call_id": "call_original",
            "output": "1",
        },
    )
    run_item_payload = {
        "type": "function_call_output",
        "call_id": "call_result",
        "output": "2",
    }
    dummy_run_item = _DummyRunItem(run_item_payload)

    await save_result_to_session(
        session,
        [original_item],
        [cast(RunItem, dummy_run_item)],
    )

    assert len(session.saved_items) == 2
    for saved in session.saved_items:
        saved_dict = cast(dict[str, Any], saved)
        assert saved_dict["type"] == "function_call_output"
        assert "output" in saved_dict


@pytest.mark.asyncio
async def test_rewind_handles_id_stripped_sessions() -> None:
    session = IdStrippingSession()
    item = cast(TResponseInputItem, {"id": "message-1", "type": "message", "content": "hello"})
    await session.add_items([item])

    await rewind_session_items(session, [item])

    assert session.pop_calls == 1
    assert session.saved_items == []


@pytest.mark.asyncio
async def test_save_result_to_session_does_not_increment_counter_when_nothing_saved() -> None:
    session = SimpleListSession()
    agent = Agent(name="agent", model=FakeModel())
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item={"type": "function_call", "call_id": "call-1", "name": "tool"},
    )

    run_state: RunState[Any] = RunState(
        context=RunContextWrapper(context={}),
        original_input="input",
        starting_agent=agent,
        max_turns=1,
    )

    await save_result_to_session(
        session,
        [],
        cast(list[RunItem], [approval_item]),
        run_state,
    )

    assert run_state._current_turn_persisted_item_count == 0
    assert session.saved_items == []


@pytest.mark.asyncio
async def test_session_persists_only_new_step_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure only per-turn new_step_items are persisted to the session."""

    session = SimpleListSession()
    agent = Agent(name="agent", model=FakeModel())

    pre_item = _DummyRunItem(
        {"type": "message", "role": "assistant", "content": "old"}, "message_output_item"
    )
    new_item = _DummyRunItem(
        {"type": "message", "role": "assistant", "content": "new"}, "message_output_item"
    )
    new_response = ModelResponse(output=[], usage=Usage(), response_id="resp-1")
    turn_result = SingleStepResult(
        original_input="hello",
        model_response=new_response,
        pre_step_items=[cast(RunItem, pre_item)],
        new_step_items=[cast(RunItem, new_item)],
        next_step=NextStepFinalOutput(output="done"),
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
    )

    calls: list[list[RunItem]] = []

    from agents.run_internal import session_persistence as sp

    real_save_result = sp.save_result_to_session

    async def save_wrapper(
        sess: Any,
        original_input: Any,
        new_items: list[RunItem],
        run_state: RunState | None = None,
        **kwargs: Any,
    ) -> None:
        calls.append(list(new_items))
        await real_save_result(sess, original_input, new_items, run_state, **kwargs)

    async def fake_run_single_turn(**_: Any) -> SingleStepResult:
        return turn_result

    async def fake_run_output_guardrails(*_: Any, **__: Any) -> list[Any]:
        return []

    async def noop_initialize_computer_tools(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("agents.run.save_result_to_session", save_wrapper)
    monkeypatch.setattr(
        "agents.run_internal.session_persistence.save_result_to_session", save_wrapper
    )
    monkeypatch.setattr("agents.run.run_single_turn", fake_run_single_turn)
    monkeypatch.setattr("agents.run_internal.run_loop.run_single_turn", fake_run_single_turn)
    monkeypatch.setattr("agents.run.run_output_guardrails", fake_run_output_guardrails)
    monkeypatch.setattr(
        "agents.run_internal.run_loop.run_output_guardrails", fake_run_output_guardrails
    )

    async def fake_get_all_tools(*_: Any, **__: Any) -> list[Any]:
        return []

    monkeypatch.setattr("agents.run.get_all_tools", fake_get_all_tools)
    monkeypatch.setattr("agents.run_internal.run_loop.get_all_tools", fake_get_all_tools)
    monkeypatch.setattr("agents.run.initialize_computer_tools", noop_initialize_computer_tools)
    monkeypatch.setattr(
        "agents.run_internal.run_loop.initialize_computer_tools", noop_initialize_computer_tools
    )

    result = await Runner.run(agent, input="hello", session=session)

    assert result.final_output == "done"
    # First save writes the user input; second save should contain only the new_step_items.
    assert len(calls) >= 2
    assert calls[-1] == [cast(RunItem, new_item)]

    items = await session.get_items()
    assert len(items) == 2
    assert any("new" in cast(dict[str, Any], item).get("content", "") for item in items)
    assert not any("old" in cast(dict[str, Any], item).get("content", "") for item in items)


@pytest.mark.asyncio
async def test_output_guardrail_tripwire_triggered_causes_exception():
    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="test",
        output_guardrails=[OutputGuardrail(guardrail_function=guardrail_function)],
        model=model,
    )
    model.set_next_output([get_text_message("user_message")])

    with pytest.raises(OutputGuardrailTripwireTriggered):
        await Runner.run(agent, input="user_message")


@pytest.mark.asyncio
async def test_input_guardrail_no_tripwire_continues_execution():
    """Test input guardrail that doesn't trigger tripwire continues execution."""

    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=False,  # Doesn't trigger tripwire
        )

    model = FakeModel()
    model.set_next_output([get_text_message("response")])

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[InputGuardrail(guardrail_function=guardrail_function)],
    )

    # Should complete successfully without raising exception
    result = await Runner.run(agent, input="user_message")
    assert result.final_output == "response"


@pytest.mark.asyncio
async def test_output_guardrail_no_tripwire_continues_execution():
    """Test output guardrail that doesn't trigger tripwire continues execution."""

    def guardrail_function(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=False,  # Doesn't trigger tripwire
        )

    model = FakeModel()
    model.set_next_output([get_text_message("response")])

    agent = Agent(
        name="test",
        model=model,
        output_guardrails=[OutputGuardrail(guardrail_function=guardrail_function)],
    )

    # Should complete successfully without raising exception
    result = await Runner.run(agent, input="user_message")
    assert result.final_output == "response"


@function_tool
def test_tool_one():
    return Foo(bar="tool_one_result")


@function_tool
def test_tool_two():
    return "tool_two_result"


@pytest.mark.asyncio
async def test_tool_use_behavior_first_output():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result"), test_tool_one, test_tool_two],
        tool_use_behavior="stop_on_first_tool",
        output_type=Foo,
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [
                get_text_message("a_message"),
                get_function_tool_call("test_tool_one", None),
                get_function_tool_call("test_tool_two", None),
            ],
        ]
    )

    result = await Runner.run(agent, input="user_message")

    assert result.final_output == Foo(bar="tool_one_result"), (
        "should have used the first tool result"
    )


def custom_tool_use_behavior(
    context: RunContextWrapper[Any], results: list[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    if "test_tool_one" in [result.tool.name for result in results]:
        return ToolsToFinalOutputResult(is_final_output=True, final_output="the_final_output")
    else:
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)


@pytest.mark.asyncio
async def test_tool_use_behavior_custom_function():
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("foo", "tool_result"), test_tool_one, test_tool_two],
        tool_use_behavior=custom_tool_use_behavior,
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [
                get_text_message("a_message"),
                get_function_tool_call("test_tool_two", None),
            ],
            # Second turn: a message and tool call
            [
                get_text_message("a_message"),
                get_function_tool_call("test_tool_one", None),
                get_function_tool_call("test_tool_two", None),
            ],
        ]
    )

    result = await Runner.run(agent, input="user_message")

    assert len(result.raw_responses) == 2, "should have two model responses"
    assert result.final_output == "the_final_output", "should have used the custom function"


@pytest.mark.asyncio
async def test_model_settings_override():
    model = FakeModel()
    agent = Agent(
        name="test", model=model, model_settings=ModelSettings(temperature=1.0, max_tokens=1000)
    )

    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("a_message"),
            ],
        ]
    )

    await Runner.run(
        agent,
        input="user_message",
        run_config=RunConfig(model_settings=ModelSettings(0.5)),
    )

    # temperature is overridden by Runner.run, but max_tokens is not
    assert model.last_turn_args["model_settings"].temperature == 0.5
    assert model.last_turn_args["model_settings"].max_tokens == 1000


@pytest.mark.asyncio
async def test_previous_response_id_passed_between_runs():
    """Test that previous_response_id is passed to the model on subsequent runs."""
    model = FakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="test", model=model)

    assert model.last_turn_args.get("previous_response_id") is None
    await Runner.run(agent, input="test", previous_response_id="resp-non-streamed-test")
    assert model.last_turn_args.get("previous_response_id") == "resp-non-streamed-test"


@pytest.mark.asyncio
async def test_multi_turn_previous_response_id_passed_between_runs():
    """Test that previous_response_id is passed to the model on subsequent runs."""

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

    assert model.last_turn_args.get("previous_response_id") is None
    await Runner.run(agent, input="test", previous_response_id="resp-test-123")
    assert model.last_turn_args.get("previous_response_id") == "resp-789"


@pytest.mark.asyncio
async def test_previous_response_id_passed_between_runs_streamed():
    """Test that previous_response_id is passed to the model on subsequent streamed runs."""
    model = FakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(
        name="test",
        model=model,
    )

    assert model.last_turn_args.get("previous_response_id") is None
    result = Runner.run_streamed(agent, input="test", previous_response_id="resp-stream-test")
    async for _ in result.stream_events():
        pass

    assert model.last_turn_args.get("previous_response_id") == "resp-stream-test"


@pytest.mark.asyncio
async def test_previous_response_id_passed_between_runs_streamed_multi_turn():
    """Test that previous_response_id is passed to the model on subsequent streamed runs."""

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

    assert model.last_turn_args.get("previous_response_id") is None
    result = Runner.run_streamed(agent, input="test", previous_response_id="resp-stream-test")
    async for _ in result.stream_events():
        pass

    assert model.last_turn_args.get("previous_response_id") == "resp-789"


@pytest.mark.asyncio
async def test_conversation_id_only_sends_new_items_multi_turn():
    """Test that conversation_id mode only sends new items on subsequent turns."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: another message and tool call
            [get_text_message("b_message"), get_function_tool_call("test_func", '{"arg": "bar"}')],
            # Third turn: final text message
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="user_message", conversation_id="conv-test-123")
    assert result.final_output == "done"

    # Check the first call - it should include the original input since generated_items is empty
    assert model.first_turn_args is not None
    first_input = model.first_turn_args["input"]

    # First call should include the original user input
    assert isinstance(first_input, list)
    assert len(first_input) == 1  # Should contain the user message

    # The input should be the user message
    user_message = first_input[0]
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # Check the input from the last turn (third turn after function execution)
    last_input = model.last_turn_args["input"]

    # In conversation_id mode, the third turn should only contain the tool output
    assert isinstance(last_input, list)
    assert len(last_input) == 1

    # The single item should be a tool result
    tool_result_item = last_input[0]
    assert tool_result_item.get("type") == "function_call_output"
    assert tool_result_item.get("call_id") is not None


@pytest.mark.asyncio
async def test_conversation_id_only_sends_new_items_multi_turn_streamed():
    """Test that conversation_id mode only sends new items on subsequent turns (streamed mode)."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: another message and tool call
            [get_text_message("b_message"), get_function_tool_call("test_func", '{"arg": "bar"}')],
            # Third turn: final text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message", conversation_id="conv-test-123")
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"

    # Check the first call - it should include the original input since generated_items is empty
    assert model.first_turn_args is not None
    first_input = model.first_turn_args["input"]

    # First call should include the original user input
    assert isinstance(first_input, list)
    assert len(first_input) == 1  # Should contain the user message

    # The input should be the user message
    user_message = first_input[0]
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # Check the input from the last turn (third turn after function execution)
    last_input = model.last_turn_args["input"]

    # In conversation_id mode, the third turn should only contain the tool output
    assert isinstance(last_input, list)
    assert len(last_input) == 1

    # The single item should be a tool result
    tool_result_item = last_input[0]
    assert tool_result_item.get("type") == "function_call_output"
    assert tool_result_item.get("call_id") is not None


@pytest.mark.asyncio
async def test_previous_response_id_only_sends_new_items_multi_turn():
    """Test that previous_response_id mode only sends new items and updates
    previous_response_id between turns."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: final text message
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(
        agent, input="user_message", previous_response_id="initial-response-123"
    )
    assert result.final_output == "done"

    # Check the first call - it should include the original input since generated_items is empty
    assert model.first_turn_args is not None
    first_input = model.first_turn_args["input"]

    # First call should include the original user input
    assert isinstance(first_input, list)
    assert len(first_input) == 1  # Should contain the user message

    # The input should be the user message
    user_message = first_input[0]
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # Check the input from the last turn (second turn after function execution)
    last_input = model.last_turn_args["input"]

    # In previous_response_id mode, the third turn should only contain the tool output
    assert isinstance(last_input, list)
    assert len(last_input) == 1  # Only the function result

    # The single item should be a tool result
    tool_result_item = last_input[0]
    assert tool_result_item.get("type") == "function_call_output"
    assert tool_result_item.get("call_id") is not None

    # Verify that previous_response_id is modified according to fake_model behavior
    assert model.last_turn_args.get("previous_response_id") == "resp-789"


@pytest.mark.asyncio
async def test_previous_response_id_only_sends_new_items_multi_turn_streamed():
    """Test that previous_response_id mode only sends new items and updates
    previous_response_id between turns (streamed mode)."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: final text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(
        agent, input="user_message", previous_response_id="initial-response-123"
    )
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"

    # Check the first call - it should include the original input since generated_items is empty
    assert model.first_turn_args is not None
    first_input = model.first_turn_args["input"]

    # First call should include the original user input
    assert isinstance(first_input, list)
    assert len(first_input) == 1  # Should contain the user message

    # The input should be the user message
    user_message = first_input[0]
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # Check the input from the last turn (second turn after function execution)
    last_input = model.last_turn_args["input"]

    # In previous_response_id mode, the third turn should only contain the tool output
    assert isinstance(last_input, list)
    assert len(last_input) == 1  # Only the function result

    # The single item should be a tool result
    tool_result_item = last_input[0]
    assert tool_result_item.get("type") == "function_call_output"
    assert tool_result_item.get("call_id") is not None

    # Verify that previous_response_id is modified according to fake_model behavior
    assert model.last_turn_args.get("previous_response_id") == "resp-789"


@pytest.mark.asyncio
async def test_default_send_all_items():
    """Test that without conversation_id or previous_response_id, all items are sent."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: final text message
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(
        agent, input="user_message"
    )  # No conversation_id or previous_response_id
    assert result.final_output == "done"

    # Check the input from the last turn (second turn after function execution)
    last_input = model.last_turn_args["input"]

    # In default, the second turn should contain ALL items:
    # 1. Original user message
    # 2. Assistant response message
    # 3. Function call
    # 4. Function result
    assert isinstance(last_input, list)
    assert (
        len(last_input) == 4
    )  # User message + assistant message + function call + function result

    # Verify the items are in the expected order
    user_message = last_input[0]
    assistant_message = last_input[1]
    function_call = last_input[2]
    function_result = last_input[3]

    # Check user message
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # Check assistant message
    assert assistant_message.get("role") == "assistant"

    # Check function call
    assert function_call.get("name") == "test_func"
    assert function_call.get("arguments") == '{"arg": "foo"}'

    # Check function result
    assert function_result.get("type") == "function_call_output"
    assert function_result.get("call_id") is not None


@pytest.mark.asyncio
async def test_default_send_all_items_streamed():
    """Test that without conversation_id or previous_response_id, all items are sent
    (streamed mode)."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: final text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(
        agent, input="user_message"
    )  # No conversation_id or previous_response_id
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"

    # Check the input from the last turn (second turn after function execution)
    last_input = model.last_turn_args["input"]

    # In default mode, the second turn should contain ALL items:
    # 1. Original user message
    # 2. Assistant response message
    # 3. Function call
    # 4. Function result
    assert isinstance(last_input, list)
    assert (
        len(last_input) == 4
    )  # User message + assistant message + function call + function result

    # Verify the items are in the expected order
    user_message = last_input[0]
    assistant_message = last_input[1]
    function_call = last_input[2]
    function_result = last_input[3]

    # Check user message
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # Check assistant message
    assert assistant_message.get("role") == "assistant"

    # Check function call
    assert function_call.get("name") == "test_func"
    assert function_call.get("arguments") == '{"arg": "foo"}'

    # Check function result
    assert function_result.get("type") == "function_call_output"
    assert function_result.get("call_id") is not None


@pytest.mark.asyncio
async def test_auto_previous_response_id_multi_turn():
    """Test that auto_previous_response_id=True enables
    chaining from the first internal turn."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: final text message
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="user_message", auto_previous_response_id=True)
    assert result.final_output == "done"

    # Check the first call
    assert model.first_turn_args is not None
    first_input = model.first_turn_args["input"]

    # First call should include the original user input
    assert isinstance(first_input, list)
    assert len(first_input) == 1  # Should contain the user message

    # The input should be the user message
    user_message = first_input[0]
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # With auto_previous_response_id=True, first call should NOT have previous_response_id
    assert model.first_turn_args.get("previous_response_id") is None

    # Check the input from the second turn (after function execution)
    last_input = model.last_turn_args["input"]

    # With auto_previous_response_id=True, the second turn should only contain the tool output
    assert isinstance(last_input, list)
    assert len(last_input) == 1  # Only the function result

    # The single item should be a tool result
    tool_result_item = last_input[0]
    assert tool_result_item.get("type") == "function_call_output"
    assert tool_result_item.get("call_id") is not None

    # With auto_previous_response_id=True, second call should have
    # previous_response_id set to the first response
    assert model.last_turn_args.get("previous_response_id") == "resp-789"


@pytest.mark.asyncio
async def test_auto_previous_response_id_multi_turn_streamed():
    """Test that auto_previous_response_id=True enables
    chaining from the first internal turn (streamed mode)."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: final text message
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="user_message", auto_previous_response_id=True)
    async for _ in result.stream_events():
        pass

    assert result.final_output == "done"

    # Check the first call
    assert model.first_turn_args is not None
    first_input = model.first_turn_args["input"]

    # First call should include the original user input
    assert isinstance(first_input, list)
    assert len(first_input) == 1  # Should contain the user message

    # The input should be the user message
    user_message = first_input[0]
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # With auto_previous_response_id=True, first call should NOT have previous_response_id
    assert model.first_turn_args.get("previous_response_id") is None

    # Check the input from the second turn (after function execution)
    last_input = model.last_turn_args["input"]

    # With auto_previous_response_id=True, the second turn should only contain the tool output
    assert isinstance(last_input, list)
    assert len(last_input) == 1  # Only the function result

    # The single item should be a tool result
    tool_result_item = last_input[0]
    assert tool_result_item.get("type") == "function_call_output"
    assert tool_result_item.get("call_id") is not None

    # With auto_previous_response_id=True, second call should have
    # previous_response_id set to the first response
    assert model.last_turn_args.get("previous_response_id") == "resp-789"


@pytest.mark.asyncio
async def test_without_previous_response_id_and_auto_previous_response_id_no_chaining():
    """Test that without previous_response_id and auto_previous_response_id,
    internal turns don't chain."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("test_func", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_func", '{"arg": "foo"}')],
            # Second turn: final text message
            [get_text_message("done")],
        ]
    )

    # Call without passing previous_response_id and without passing auto_previous_response_id
    result = await Runner.run(agent, input="user_message")
    assert result.final_output == "done"

    # Check the first call
    assert model.first_turn_args is not None
    first_input = model.first_turn_args["input"]

    # First call should include the original user input
    assert isinstance(first_input, list)
    assert len(first_input) == 1  # Should contain the user message

    # The input should be the user message
    user_message = first_input[0]
    assert user_message.get("role") == "user"
    assert user_message.get("content") == "user_message"

    # First call should NOT have previous_response_id
    assert model.first_turn_args.get("previous_response_id") is None

    # Check the input from the second turn (after function execution)
    last_input = model.last_turn_args["input"]

    # Without passing previous_response_id and auto_previous_response_id,
    # the second turn should contain all items (no chaining):
    # user message, assistant response, function call, and tool result
    assert isinstance(last_input, list)
    assert len(last_input) == 4  # User message, assistant message, function call, and tool result

    # Second call should also NOT have previous_response_id (no chaining)
    assert model.last_turn_args.get("previous_response_id") is None


@pytest.mark.asyncio
async def test_dynamic_tool_addition_run() -> None:
    """Test that tools can be added to an agent during a run."""
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

    result = await Runner.run(agent, input="start")

    assert executed["called"] is True
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_session_add_items_called_multiple_times_for_multi_turn_completion():
    """Test that SQLiteSession.add_items is called multiple times
    during a multi-turn agent completion.

    """
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_agent_runner_session_multi_turn_calls.db"
        session_id = "runner_session_multi_turn_calls"
        session = SQLiteSession(session_id, db_path)

        # Define a tool that will be called by the orchestrator agent
        @function_tool
        async def echo_tool(text: str) -> str:
            return f"Echo: {text}"

        # Orchestrator agent that calls the tool multiple times in one completion
        orchestrator_agent = Agent(
            name="orchestrator_agent",
            instructions=(
                "Call echo_tool twice with inputs of 'foo' and 'bar', then return a summary."
            ),
            tools=[echo_tool],
        )

        # Patch the model to simulate two tool calls and a final message
        model = FakeModel()
        orchestrator_agent.model = model
        model.add_multiple_turn_outputs(
            [
                # First turn: tool call
                [get_function_tool_call("echo_tool", json.dumps({"text": "foo"}), call_id="1")],
                # Second turn: tool call
                [get_function_tool_call("echo_tool", json.dumps({"text": "bar"}), call_id="2")],
                # Third turn: final output
                [get_final_output_message("Summary: Echoed foo and bar")],
            ]
        )

        # Patch add_items to count calls
        with patch.object(SQLiteSession, "add_items", wraps=session.add_items) as mock_add_items:
            result = await Runner.run(orchestrator_agent, input="foo and bar", session=session)

            expected_items = [
                {"content": "foo and bar", "role": "user"},
                {
                    "arguments": '{"text": "foo"}',
                    "call_id": "1",
                    "name": "echo_tool",
                    "type": "function_call",
                    "id": "1",
                },
                {"call_id": "1", "output": "Echo: foo", "type": "function_call_output"},
                {
                    "arguments": '{"text": "bar"}',
                    "call_id": "2",
                    "name": "echo_tool",
                    "type": "function_call",
                    "id": "1",
                },
                {"call_id": "2", "output": "Echo: bar", "type": "function_call_output"},
                {
                    "id": "1",
                    "content": [
                        {
                            "annotations": [],
                            "logprobs": [],
                            "text": "Summary: Echoed foo and bar",
                            "type": "output_text",
                        }
                    ],
                    "role": "assistant",
                    "status": "completed",
                    "type": "message",
                },
            ]

            expected_calls = [
                # First call is the initial input
                (([expected_items[0]],),),
                # Second call is the first tool call and its result
                (([expected_items[1], expected_items[2]],),),
                # Third call is the second tool call and its result
                (([expected_items[3], expected_items[4]],),),
                # Fourth call is the final output
                (([expected_items[5]],),),
            ]
            assert mock_add_items.call_args_list == expected_calls
            assert result.final_output == "Summary: Echoed foo and bar"
            assert (await session.get_items()) == expected_items

        session.close()


@pytest.mark.asyncio
async def test_execute_approved_tools_with_non_function_tool():
    """Test _execute_approved_tools handles non-FunctionTool."""
    model = FakeModel()

    # Create a computer tool (not a FunctionTool)
    class MockComputer(Computer):
        @property
        def environment(self) -> str:  # type: ignore[override]
            return "mac"

        @property
        def dimensions(self) -> tuple[int, int]:
            return (1920, 1080)

        def screenshot(self) -> str:
            return "screenshot"

        def click(self, x: int, y: int, button: str) -> None:
            pass

        def double_click(self, x: int, y: int) -> None:
            pass

        def drag(self, path: list[tuple[int, int]]) -> None:
            pass

        def keypress(self, keys: list[str]) -> None:
            pass

        def move(self, x: int, y: int) -> None:
            pass

        def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
            pass

        def type(self, text: str) -> None:
            pass

        def wait(self) -> None:
            pass

    computer = MockComputer()
    computer_tool = ComputerTool(computer=computer)

    agent = Agent(name="TestAgent", model=model, tools=[computer_tool])

    # Create an approved tool call for the computer tool
    # ComputerTool has name "computer_use_preview"
    tool_call = get_function_tool_call("computer_use_preview", "{}")
    assert isinstance(tool_call, ResponseFunctionToolCall)

    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)

    generated_items = await run_execute_approved_tools(
        agent=agent,
        approval_item=approval_item,
        approve=True,
    )

    # Should add error message about tool not being a function tool
    assert len(generated_items) == 1
    assert isinstance(generated_items[0], ToolCallOutputItem)
    assert "not a function tool" in generated_items[0].output.lower()


@pytest.mark.asyncio
async def test_execute_approved_tools_with_rejected_tool():
    """Test _execute_approved_tools handles rejected tools."""
    tool_called = False

    async def test_tool() -> str:
        nonlocal tool_called
        tool_called = True
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool")
    _, agent = make_model_and_agent(tools=[tool])

    # Create a rejected tool call
    tool_call = get_function_tool_call("test_tool", "{}")
    assert isinstance(tool_call, ResponseFunctionToolCall)
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)

    generated_items = await run_execute_approved_tools(
        agent=agent,
        approval_item=approval_item,
        approve=False,
    )

    # Should add rejection message
    assert len(generated_items) == 1
    assert "not approved" in generated_items[0].output.lower()
    assert not tool_called  # Tool should not have been executed


@pytest.mark.asyncio
async def test_execute_approved_tools_with_unclear_status():
    """Test _execute_approved_tools handles unclear approval status."""
    tool_called = False

    async def test_tool() -> str:
        nonlocal tool_called
        tool_called = True
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool")
    _, agent = make_model_and_agent(tools=[tool])

    # Create a tool call with unclear status (neither approved nor rejected)
    tool_call = get_function_tool_call("test_tool", "{}")
    assert isinstance(tool_call, ResponseFunctionToolCall)
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)

    generated_items = await run_execute_approved_tools(
        agent=agent,
        approval_item=approval_item,
        approve=None,
    )

    # Should add unclear status message
    assert len(generated_items) == 1
    assert "unclear" in generated_items[0].output.lower()
    assert not tool_called  # Tool should not have been executed


@pytest.mark.asyncio
async def test_execute_approved_tools_with_missing_tool():
    """Test _execute_approved_tools handles missing tools."""
    _, agent = make_model_and_agent()
    # Agent has no tools

    # Create an approved tool call for a tool that doesn't exist
    tool_call = get_function_tool_call("nonexistent_tool", "{}")
    assert isinstance(tool_call, ResponseFunctionToolCall)
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)

    generated_items = await run_execute_approved_tools(
        agent=agent,
        approval_item=approval_item,
        approve=True,
    )

    # Should add error message about tool not found
    assert len(generated_items) == 1
    assert isinstance(generated_items[0], ToolCallOutputItem)
    assert "not found" in generated_items[0].output.lower()


@pytest.mark.asyncio
async def test_execute_approved_tools_instance_method():
    """Ensure execute_approved_tools runs approved tools as expected."""
    tool_called = False

    async def test_tool() -> str:
        nonlocal tool_called
        tool_called = True
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool")
    _, agent = make_model_and_agent(tools=[tool])

    tool_call = get_function_tool_call("test_tool", json.dumps({}))
    assert isinstance(tool_call, ResponseFunctionToolCall)

    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)

    generated_items = await run_execute_approved_tools(
        agent=agent,
        approval_item=approval_item,
        approve=True,
    )

    # Tool should have been called
    assert tool_called is True
    assert len(generated_items) == 1
    assert isinstance(generated_items[0], ToolCallOutputItem)
    assert generated_items[0].output == "tool_result"
