"""Tests for on_turn_start / on_turn_end lifecycle hooks (issue #2671)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

import pytest

from agents import Agent, Runner
from agents.items import ModelResponse, TResponseInputItem
from agents.lifecycle import AgentHooks, RunHooks
from agents.run_context import RunContextWrapper, TContext
from agents.tool import FunctionTool, Tool

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


class TurnTrackingRunHooks(RunHooks):
    """Records turn numbers seen by on_turn_start and on_turn_end."""

    def __init__(self) -> None:
        """Initialise empty tracking lists and event counters."""
        self.turn_starts: list[int] = []
        self.turn_ends: list[int] = []
        self.events: dict[str, int] = defaultdict(int)

    async def on_turn_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Any,
        turn_number: int,
    ) -> None:
        """Record the turn number when a turn starts."""
        self.turn_starts.append(turn_number)
        self.events["on_turn_start"] += 1

    async def on_turn_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Any,
        turn_number: int,
    ) -> None:
        """Record the turn number when a turn ends."""
        self.turn_ends.append(turn_number)
        self.events["on_turn_end"] += 1


class TurnTrackingAgentHooks(AgentHooks):
    """Records turn numbers seen on agent-level hooks."""

    def __init__(self) -> None:
        """Initialise empty tracking lists."""
        self.turn_starts: list[int] = []
        self.turn_ends: list[int] = []

    async def on_turn_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Any,
        turn_number: int,
    ) -> None:
        """Record the turn number when a turn starts."""
        self.turn_starts.append(turn_number)

    async def on_turn_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Any,
        turn_number: int,
    ) -> None:
        """Record the turn number when a turn ends."""
        self.turn_ends.append(turn_number)


@pytest.mark.asyncio
async def test_on_turn_start_and_end_single_turn() -> None:
    """on_turn_start and on_turn_end are both called once for a single-turn run."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    hooks = TurnTrackingRunHooks()
    agent = Agent(name="A", model=model)

    await Runner.run(agent, input="hi", hooks=hooks)

    assert hooks.turn_starts == [1]
    assert hooks.turn_ends == [1]


@pytest.mark.asyncio
async def test_on_turn_numbers_multi_turn() -> None:
    """Turn numbers increment correctly across multiple turns."""
    model = FakeModel()
    # Turn 1: model calls a tool; turn 2: model produces final output.
    tool = get_function_tool("my_tool", "tool_result")
    model.add_multiple_turn_outputs([
        [get_function_tool_call("my_tool", "{}")],
        [get_text_message("done")],
    ])

    hooks = TurnTrackingRunHooks()
    agent = Agent(name="A", model=model, tools=[tool])

    await Runner.run(agent, input="hi", hooks=hooks)

    assert hooks.turn_starts == [1, 2]
    assert hooks.turn_ends == [1, 2]


@pytest.mark.asyncio
async def test_on_turn_start_fires_before_llm() -> None:
    """on_turn_start fires before the LLM call each turn."""
    call_order: list[str] = []

    class OrderTrackingHooks(RunHooks):
        """Tracks the order that turn/LLM lifecycle hooks are called."""

        async def on_turn_start(self, context: Any, agent: Any, turn_number: int) -> None:
            """Append a turn_start marker with the turn number."""
            call_order.append(f"turn_start:{turn_number}")

        async def on_llm_start(self, context: Any, agent: Any, system_prompt: Any, input_items: Any) -> None:
            """Append an llm_start marker."""
            call_order.append("llm_start")

        async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
            """Append an llm_end marker."""
            call_order.append("llm_end")

        async def on_turn_end(self, context: Any, agent: Any, turn_number: int) -> None:
            """Append a turn_end marker with the turn number."""
            call_order.append(f"turn_end:{turn_number}")

    model = FakeModel()
    model.set_next_output([get_text_message("hello")])
    hooks = OrderTrackingHooks()
    agent = Agent(name="A", model=model)

    await Runner.run(agent, input="hi", hooks=hooks)

    # turn_start must come before llm_start, llm_end before turn_end
    ts_idx = call_order.index("turn_start:1")
    ls_idx = call_order.index("llm_start")
    le_idx = call_order.index("llm_end")
    te_idx = call_order.index("turn_end:1")

    assert ts_idx < ls_idx
    assert ls_idx < le_idx
    assert le_idx < te_idx


@pytest.mark.asyncio
async def test_agent_level_on_turn_start_and_end() -> None:
    """Agent-level on_turn_start / on_turn_end hooks are also called."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    agent_hooks = TurnTrackingAgentHooks()
    agent = Agent(name="A", model=model, hooks=agent_hooks)

    await Runner.run(agent, input="hi")

    assert agent_hooks.turn_starts == [1]
    assert agent_hooks.turn_ends == [1]


@pytest.mark.asyncio
async def test_run_and_agent_hooks_both_called() -> None:
    """Both run-level and agent-level hooks fire for the same turn."""
    model = FakeModel()
    model.set_next_output([get_text_message("hi")])

    run_hooks = TurnTrackingRunHooks()
    agent_hooks = TurnTrackingAgentHooks()
    agent = Agent(name="A", model=model, hooks=agent_hooks)

    await Runner.run(agent, input="hi", hooks=run_hooks)

    assert run_hooks.turn_starts == [1]
    assert run_hooks.turn_ends == [1]
    assert agent_hooks.turn_starts == [1]
    assert agent_hooks.turn_ends == [1]


@pytest.mark.asyncio
async def test_on_turn_hooks_with_streaming() -> None:
    """on_turn_start and on_turn_end are called when using the streaming runner."""
    model = FakeModel()
    model.set_next_output([get_text_message("streamed")])

    hooks = TurnTrackingRunHooks()
    agent = Agent(name="A", model=model)

    result = Runner.run_streamed(agent, input="hi", hooks=hooks)
    async for _ in result.stream_events():
        pass

    assert hooks.turn_starts == [1]
    assert hooks.turn_ends == [1]
