from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest
from typing_extensions import TypedDict

from agents import Agent, RunContextWrapper, RunHooks, Runner, TContext, Tool

from .fake_model import FakeModel
from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_message,
)


class RunHooksForTests(RunHooks):
    def __init__(self):
        self.events: dict[str, int] = defaultdict(int)

    def reset(self):
        self.events.clear()

    async def on_agent_start(
        self, context: RunContextWrapper[TContext], agent: Agent[TContext]
    ) -> None:
        self.events["on_agent_start"] += 1

    async def on_agent_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        output: Any,
    ) -> None:
        self.events["on_agent_end"] += 1

    async def on_handoff(
        self,
        context: RunContextWrapper[TContext],
        from_agent: Agent[TContext],
        to_agent: Agent[TContext],
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
    hooks = RunHooksForTests()
    model = FakeModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
    )

    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_text_message("user_message")])
    output = await Runner.run(agent_3, input="user_message", hooks=hooks)
    assert hooks.events == {"on_agent_start": 1, "on_agent_end": 1}, f"{output}"
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
    await Runner.run(agent_3, input="user_message", hooks=hooks)
    assert hooks.events == {
        # We only invoke on_agent_start when we begin executing a new agent.
        # Although agent_3 runs two turns internally before handing off,
        # that's one logical agent segment, so on_agent_start fires once.
        # Then we hand off to agent_1, so on_agent_start fires for that agent.
        "on_agent_start": 2,
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_agent_end": 1,  # Should always have one end
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
    await Runner.run(agent_3, input="user_message", hooks=hooks)

    assert hooks.events == {
        # agent_3 starts (fires on_agent_start), runs two turns and hands off.
        # agent_1 starts (fires on_agent_start), then hands back to agent_3.
        # agent_3 starts again (fires on_agent_start) to complete execution.
        "on_agent_start": 3,
        "on_tool_start": 2,  # 2 tool calls
        "on_tool_end": 2,  # 2 tool calls
        "on_handoff": 2,  # 2 handoffs
        "on_agent_end": 1,  # Should always have one end
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


@pytest.mark.asyncio
async def test_streamed_agent_hooks():
    hooks = RunHooksForTests()
    model = FakeModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
    )

    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_text_message("user_message")])
    output = Runner.run_streamed(agent_3, input="user_message", hooks=hooks)
    async for _ in output.stream_events():
        pass
    assert hooks.events == {"on_agent_start": 1, "on_agent_end": 1}, f"{output}"
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
    output = Runner.run_streamed(agent_3, input="user_message", hooks=hooks)
    async for _ in output.stream_events():
        pass
    assert hooks.events == {
        # As in the non-streamed case above, two logical agent segments:
        # starting agent_3, then handoff to agent_1.
        "on_agent_start": 2,
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_agent_end": 1,  # Should always have one end
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
    output = Runner.run_streamed(agent_3, input="user_message", hooks=hooks)
    async for _ in output.stream_events():
        pass

    assert hooks.events == {
        # Same three logical agent segments as in the non-streamed case,
        # so on_agent_start fires three times.
        "on_agent_start": 3,
        "on_tool_start": 2,  # 2 tool calls
        "on_tool_end": 2,  # 2 tool calls
        "on_handoff": 2,  # 2 handoffs
        "on_agent_end": 1,  # Should always have one end
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


class Foo(TypedDict):
    a: str


@pytest.mark.asyncio
async def test_structured_output_non_streamed_agent_hooks():
    hooks = RunHooksForTests()
    model = FakeModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        output_type=Foo,
    )

    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_final_output_message(json.dumps({"a": "b"}))])
    output = await Runner.run(agent_3, input="user_message", hooks=hooks)
    assert hooks.events == {"on_agent_start": 1, "on_agent_end": 1}, f"{output}"
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
    output = await Runner.run(agent_3, input="user_message", hooks=hooks)

    assert hooks.events == {
        # As with unstructured output, we expect on_agent_start once for
        # agent_3 and once for agent_1.
        "on_agent_start": 2,
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_agent_end": 1,  # Should always have one end
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
    await Runner.run(agent_3, input="user_message", hooks=hooks)

    assert hooks.events == {
        # We still expect three logical agent segments, as before.
        "on_agent_start": 3,
        "on_tool_start": 2,  # 2 tool calls
        "on_tool_end": 2,  # 2 tool calls
        "on_handoff": 2,  # 2 handoffs
        "on_agent_end": 1,  # Should always have one end
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()


@pytest.mark.asyncio
async def test_structured_output_streamed_agent_hooks():
    hooks = RunHooksForTests()
    model = FakeModel()
    agent_1 = Agent(name="test_1", model=model)
    agent_2 = Agent(name="test_2", model=model)
    agent_3 = Agent(
        name="test_3",
        model=model,
        handoffs=[agent_1, agent_2],
        tools=[get_function_tool("some_function", "result")],
        output_type=Foo,
    )

    agent_1.handoffs.append(agent_3)

    model.set_next_output([get_final_output_message(json.dumps({"a": "b"}))])
    output = Runner.run_streamed(agent_3, input="user_message", hooks=hooks)
    async for _ in output.stream_events():
        pass
    assert hooks.events == {"on_agent_start": 1, "on_agent_end": 1}, f"{output}"
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
    output = Runner.run_streamed(agent_3, input="user_message", hooks=hooks)
    async for _ in output.stream_events():
        pass

    assert hooks.events == {
        # Two agent segments: agent_3 and then agent_1.
        "on_agent_start": 2,
        "on_tool_start": 1,  # Only one tool call
        "on_tool_end": 1,  # Only one tool call
        "on_handoff": 1,  # Only one handoff
        "on_agent_end": 1,  # Should always have one end
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
    output = Runner.run_streamed(agent_3, input="user_message", hooks=hooks)
    async for _ in output.stream_events():
        pass

    assert hooks.events == {
        # Three agent segments: agent_3, agent_1, agent_3 again.
        "on_agent_start": 3,
        "on_tool_start": 2,  # 2 tool calls
        "on_tool_end": 2,  # 2 tool calls
        "on_handoff": 2,  # 2 handoffs
        "on_agent_end": 1,  # Should always have one end
    }, f"got unexpected event count: {hooks.events}"
    hooks.reset()
