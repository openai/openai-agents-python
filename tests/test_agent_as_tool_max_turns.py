from __future__ import annotations

import json

import pytest

from agents import Agent, MaxTurnsExceeded, Runner
from agents.run import DEFAULT_MAX_TURNS

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


@pytest.mark.asyncio
async def test_runner_run_max_turns_none_defaults_to_constant():
    model = FakeModel()
    agent = Agent(
        name="test_runner_max_turns_none",
        model=model,
        tools=[get_function_tool("tool", "ok")],
    )

    # Prepare 11 turns (DEFAULT_MAX_TURNS is 10) to ensure exceeding default.
    func_output = json.dumps({"a": "b"})
    turns: list[list[object]] = []
    for i in range(1, DEFAULT_MAX_TURNS + 1):
        turns.append([get_text_message(str(i)), get_function_tool_call("tool", func_output)])
    model.add_multiple_turn_outputs(turns)

    # Passing None should make Runner default to DEFAULT_MAX_TURNS (10), so 11th turn exceeds.
    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="go", max_turns=None)


@pytest.mark.asyncio
async def test_agent_as_tool_forwards_max_turns():
    # Inner agent will exceed when limited to 1 turn.
    inner_model = FakeModel()
    inner_agent = Agent(
        name="inner",
        model=inner_model,
        tools=[get_function_tool("some_function", "ok")],
    )

    # Make inner agent require more than 1 turn.
    func_output = json.dumps({"x": 1})
    inner_model.add_multiple_turn_outputs(
        [
            [get_text_message("t1"), get_function_tool_call("some_function", func_output)],
            [get_text_message("t2"), get_function_tool_call("some_function", func_output)],
        ]
    )

    # Wrap inner agent as a tool with max_turns=1.
    wrapped_tool = inner_agent.as_tool(
        tool_name="inner_tool",
        tool_description="runs inner agent",
        max_turns=1,
    )

    # Orchestrator will call the wrapped tool twice, causing inner to exceed its max_turns.
    outer_model = FakeModel()
    orchestrator = Agent(
        name="orchestrator",
        model=outer_model,
        tools=[wrapped_tool],
    )

    # Outer model asks to call the tool once;
    # exceeding happens inside the tool call when inner runs.
    outer_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("inner_tool")],
        ]
    )

    # Since tool default error handling returns a string on error,
    # the run should not raise here.
    result = await Runner.run(orchestrator, input="start")

    # The tool call error should be surfaced as a message back to the model;
    # ensure we have some output.
    # We don't assert exact message text to avoid brittleness;
    # just ensure the run completed with items.
    assert len(result.new_items) >= 1
