from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
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
from agents.tool import FunctionToolResult, function_tool

from .fake_model import FakeModel
from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)


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
        "should have 7 inputs: orig input, tool call, tool result, message, handoff, handoff"
        "result, and done message"
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
    assert len(result.to_input_list()) == 11, (
        "should have input: 2 orig inputs, function call, function call result, message, handoff, "
        "handoff output, preamble message, tool call, tool call result, final output"
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

    # In defaul, the second turn should contain ALL items:
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
