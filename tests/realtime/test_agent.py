from __future__ import annotations

from typing import Any

import pytest

from agents import RunContextWrapper
from agents.realtime.agent import RealtimeAgent


def test_can_initialize_realtime_agent():
    agent = RealtimeAgent(name="test", instructions="Hello")
    assert agent.name == "test"
    assert agent.instructions == "Hello"


@pytest.mark.asyncio
async def test_dynamic_instructions():
    agent = RealtimeAgent(name="test")
    assert agent.instructions is None

    def _instructions(ctx, agt) -> str:
        assert ctx.context is None
        assert agt == agent
        return "Dynamic"

    agent = RealtimeAgent(name="test", instructions=_instructions)
    instructions = await agent.get_system_prompt(RunContextWrapper(context=None))
    assert instructions == "Dynamic"


def test_post_init_rejects_invalid_field_types() -> None:
    with pytest.raises(TypeError, match="RealtimeAgent name must be a string"):
        RealtimeAgent(name=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RealtimeAgent tools must be a list"):
        RealtimeAgent(name="x", tools="nope")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RealtimeAgent handoffs must be a list"):
        RealtimeAgent(name="x", handoffs="nope")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RealtimeAgent instructions must be"):
        RealtimeAgent(name="x", instructions=123)  # type: ignore[arg-type]


def test_clone_does_not_mutate_original_lists() -> None:
    """Cloning with a new list must not affect the original agent's lists."""
    original = RealtimeAgent(name="orig", tools=[], handoffs=[])
    new_tools: list[Any] = ["t1"]
    cloned = original.clone(tools=new_tools)
    assert original.tools == []
    assert len(cloned.tools) == 1
    assert cloned.tools is not original.tools
    # Shared reference when not overridden (documented shallow-copy behavior).
    assert cloned.handoffs is original.handoffs
