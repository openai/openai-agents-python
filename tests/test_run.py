from __future__ import annotations

from unittest import mock

import pytest

from agents import Agent, ModelBehaviorError, Runner
from agents.run import AgentRunner, set_default_agent_runner

from .fake_model import FakeModel
from .test_responses import (
    get_function_tool,
    get_function_tool_call,
    get_text_input_item,
    get_text_message,
)


@pytest.mark.asyncio
async def test_static_run_methods_call_into_default_runner() -> None:
    runner = mock.Mock(spec=AgentRunner)
    set_default_agent_runner(runner)

    agent = Agent(name="test", model=FakeModel())
    await Runner.run(agent, input="test")
    runner.run.assert_called_once()

    Runner.run_streamed(agent, input="test")
    runner.run_streamed.assert_called_once()

    Runner.run_sync(agent, input="test")
    runner.run_sync.assert_called_once()


@pytest.mark.asyncio
async def test_run_preserves_duplicate_user_messages() -> None:
    model = FakeModel()
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="test", model=model)

    input_items = [get_text_input_item("repeat"), get_text_input_item("repeat")]

    await Runner.run(agent, input=input_items)

    sent_input = model.last_turn_args["input"]
    assert isinstance(sent_input, list)
    assert len(sent_input) == 2
    assert sent_input[0]["content"] == "repeat"
    assert sent_input[1]["content"] == "repeat"


@pytest.mark.asyncio
async def test_unknown_tool_default_raises_model_behavior_error() -> None:
    """Default Agent still raises ModelBehaviorError when the model calls a missing tool."""
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("does_not_exist", "")],
            [get_text_message("unreachable")],
        ]
    )
    agent = Agent(name="test", model=model, tools=[get_function_tool("known", "ok")])

    with pytest.raises(ModelBehaviorError, match="does_not_exist"):
        await Runner.run(agent, input="hello")


@pytest.mark.asyncio
async def test_unknown_tool_respond_lets_run_continue() -> None:
    """With unknown_tool_behavior='respond', the run continues and the model can recover."""
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("does_not_exist", "")],
            [get_text_message("recovered")],
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        tools=[get_function_tool("known", "ok")],
        unknown_tool_behavior="respond",
    )

    result = await Runner.run(agent, input="hello")

    assert result.final_output == "recovered"
    # The second model turn must have been fed the synthetic recovery tool output.
    sent_input = model.last_turn_args["input"]
    assert isinstance(sent_input, list)
    function_call_outputs = [
        item
        for item in sent_input
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    assert function_call_outputs, "expected a synthetic function_call_output for the unknown tool"
    output_text = function_call_outputs[-1].get("output")
    assert isinstance(output_text, str)
    assert "does_not_exist" in output_text
    assert "known" in output_text
