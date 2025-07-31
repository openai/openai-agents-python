from __future__ import annotations

import json
from typing import Any

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
            # Third turn: tool call and structured output
            [
                get_function_tool_call("bar", json.dumps({"bar": "baz"})),
                get_final_output_message(json.dumps(Foo(bar="baz"))),
            ],
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
    assert len(result.raw_responses) == 3, "should have three model responses"
    assert len(result.to_input_list()) == 10, (
        "should have input: 2 orig inputs, function call, function call result, message, handoff, "
        "handoff output, tool call, tool call result, final output message"
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
    assert model.last_turn_args.get("previous_response_id") == "resp-test-123"


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

    assert model.last_turn_args.get("previous_response_id") == "resp-stream-test"


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
