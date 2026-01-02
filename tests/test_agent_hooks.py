from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest
from typing_extensions import TypedDict

from agents.agent import Agent
from agents.lifecycle import AgentHooks
from agents.run import Runner
from agents.run_context import AgentHookContext, RunContextWrapper, TContext
from agents.tool import Tool

from .fake_model import FakeModel
from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_message,
)


class AgentHooksForTests(AgentHooks):
    def __init__(self):
        self.events: dict[str, int] = defaultdict(int)

    def reset(self):
        self.events.clear()

    async def on_start(self, context: AgentHookContext[TContext], agent: Agent[TContext]) -> None:
        self.events["on_start"] += 1

    async def on_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        output: Any,
    ) -> None:
        self.events["on_end"] += 1

    async def on_handoff(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        source: Agent[TContext],
    ) -> None:
        self.events["on_handoff"] += 1

    async def on_tool_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
    ) -> None:
        self.events["on_tool_start"] += 1

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
        result: str,
    ) -> None:
        self.events["on_tool_end"] += 1


@pytest.mark.asyncio
async def test_non_streamed_agent_hooks():
    hooks = AgentHooksForTests()
    model = FakeModel()
    agent_1 = Agent(
        name="test_1",
        model=model,
    )
    agent_2 = Agent(
        name="test_2",
        model=model,
    )
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
    )

    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_text_message("user_message")])
    output = await Runner.run(agent_3, input="user_message")
    assert hooks.events == {"on_start": 1, "on_end": 1}, f"{output}"
    hooks.reset()

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
    await Runner.run(agent_3, input="user_message")

    # Shouldn't have on_end because it's not the last agent
    assert hooks.events == {
        "on_start": 1,  # Agent runs once
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"})),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: text message
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    assert hooks.events == {
        "on_start": 2,  # Agent runs twice
        "on_tool_start": 2,  # Only one tool call
        "on_tool_end": 2,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_end": 1,  # Agent 3 is the last agent
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


@pytest.mark.asyncio
async def test_streamed_agent_hooks():
    hooks = AgentHooksForTests()
    model = FakeModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
    )

    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_text_message("user_message")])
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass
    assert hooks.events == {"on_start": 1, "on_end": 1}, f"{output}"
    hooks.reset()

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
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass

    # Shouldn't have on_end because it's not the last agent
    assert hooks.events == {
        "on_start": 1,  # Agent runs twice
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"})),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: text message
            [get_text_message("done")],
        ]
    )
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass

    assert hooks.events == {
        "on_start": 2,  # Agent runs twice
        "on_tool_start": 2,  # Only one tool call
        "on_tool_end": 2,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_end": 1,  # Agent 3 is the last agent
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


class Foo(TypedDict):
    a: str


@pytest.mark.asyncio
async def test_structured_output_non_streamed_agent_hooks():
    hooks = AgentHooksForTests()
    model = FakeModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
        output_type=Foo,
    )

    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_final_output_message(json.dumps({"a": "b"}))])
    output = await Runner.run(agent_3, input="user_message")
    assert hooks.events == {"on_start": 1, "on_end": 1}, f"{output}"
    hooks.reset()

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: end message (for agent 1)
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    # Shouldn't have on_end because it's not the last agent
    assert hooks.events == {
        "on_start": 1,  # Agent runs twice
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"})),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: end message (for agent 3)
            [get_final_output_message(json.dumps({"a": "b"}))],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    assert hooks.events == {
        "on_start": 2,  # Agent runs twice
        "on_tool_start": 2,  # Only one tool call
        "on_tool_end": 2,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_end": 1,  # Agent 3 is the last agent
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


@pytest.mark.asyncio
async def test_structured_output_streamed_agent_hooks():
    hooks = AgentHooksForTests()
    model = FakeModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
        output_type=Foo,
    )

    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_final_output_message(json.dumps({"a": "b"}))])
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass
    assert hooks.events == {"on_start": 1, "on_end": 1}, f"{output}"
    hooks.reset()

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: end message (for agent 1)
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")
    # Shouldn't have on_end because it's not the last agent
    assert hooks.events == {
        "on_start": 1,  # Agent runs twice
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"})),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: end message (for agent 3)
            [get_final_output_message(json.dumps({"a": "b"}))],
        ]
    )
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass

    assert hooks.events == {
        "on_start": 2,  # Agent runs twice
        "on_tool_start": 2,  # 2 tool calls
        "on_tool_end": 2,  # 2 tool calls
        "on_handoff": 1,  # 1 handoff
        "on_end": 1,  # Agent 3 is the last agent
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


class EmptyAgentHooks(AgentHooks):
    pass


@pytest.mark.asyncio
async def test_base_agent_hooks_dont_crash():
    hooks = EmptyAgentHooks()
    model = FakeModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        hooks=hooks,
        output_type=Foo,
    )
    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_final_output_message(json.dumps({"a": "b"}))])
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message and a handoff
            [get_text_message("a_message"), get_handoff_tool_call(agent_1)],
            # Third turn: end message (for agent 1)
            [get_text_message("done")],
        ]
    )
    await Runner.run(agent_3, input="user_message")

    model.add_multiple_turn_outputs(
        [
            # First turn: a tool call
            [get_function_tool_call("some_function", json.dumps({"a": "b"}))],
            # Second turn: a message, another tool call, and a handoff
            [
                get_text_message("a_message"),
                get_function_tool_call("some_function", json.dumps({"a": "b"})),
                get_handoff_tool_call(agent_1),
            ],
            # Third turn: a message and a handoff back to the orig agent
            [get_text_message("a_message"), get_handoff_tool_call(agent_3)],
            # Fourth turn: end message (for agent 3)
            [get_final_output_message(json.dumps({"a": "b"}))],
        ]
    )
    output = Runner.run_streamed(agent_3, input="user_message")
    async for _ in output.stream_events():
        pass


class AgentHooksWithTurnInput(AgentHooks):
    """Agent hooks that capture turn_input from on_start."""

    def __init__(self):
        self.captured_turn_inputs: list[list[Any]] = []

    async def on_start(self, context: AgentHookContext[TContext], agent: Agent[TContext]) -> None:
        self.captured_turn_inputs.append(list(context.turn_input))


@pytest.mark.asyncio
async def test_agent_hooks_receives_turn_input_string():
    """Test that on_start receives turn_input when input is a string."""
    hooks = AgentHooksWithTurnInput()
    model = FakeModel()
    agent = Agent(name="test", model=model, hooks=hooks)

    model.set_next_output([get_text_message("response")])
    await Runner.run(agent, input="hello world")

    assert len(hooks.captured_turn_inputs) == 1
    turn_input = hooks.captured_turn_inputs[0]
    assert len(turn_input) == 1
    assert turn_input[0]["content"] == "hello world"
    assert turn_input[0]["role"] == "user"


@pytest.mark.asyncio
async def test_agent_hooks_receives_turn_input_list():
    """Test that on_start receives turn_input when input is a list."""
    hooks = AgentHooksWithTurnInput()
    model = FakeModel()
    agent = Agent(name="test", model=model, hooks=hooks)

    input_items: list[Any] = [
        {"role": "user", "content": "first message"},
        {"role": "user", "content": "second message"},
    ]

    model.set_next_output([get_text_message("response")])
    await Runner.run(agent, input=input_items)

    assert len(hooks.captured_turn_inputs) == 1
    turn_input = hooks.captured_turn_inputs[0]
    assert len(turn_input) == 2
    assert turn_input[0]["content"] == "first message"
    assert turn_input[1]["content"] == "second message"


@pytest.mark.asyncio
async def test_agent_hooks_receives_turn_input_streamed():
    """Test that on_start receives turn_input in streamed mode."""
    hooks = AgentHooksWithTurnInput()
    model = FakeModel()
    agent = Agent(name="test", model=model, hooks=hooks)

    model.set_next_output([get_text_message("response")])
    result = Runner.run_streamed(agent, input="streamed input")
    async for _ in result.stream_events():
        pass

    assert len(hooks.captured_turn_inputs) == 1
    turn_input = hooks.captured_turn_inputs[0]
    assert len(turn_input) == 1
    assert turn_input[0]["content"] == "streamed input"
