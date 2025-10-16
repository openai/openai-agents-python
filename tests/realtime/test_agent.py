from __future__ import annotations

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


def test_agent_model_parameter():
    """Test that model parameter can be set on RealtimeAgent."""
    # Agent without model (default)
    agent_default = RealtimeAgent(name="test", instructions="Hello")
    assert agent_default.model is None

    # Agent with specific model
    agent_with_model = RealtimeAgent(
        name="test", instructions="Hello", model="gpt-4o-realtime-preview"
    )
    assert agent_with_model.model == "gpt-4o-realtime-preview"

    # Agent with gpt-realtime model
    agent_gpt_realtime = RealtimeAgent(name="test", instructions="Hello", model="gpt-realtime")
    assert agent_gpt_realtime.model == "gpt-realtime"


def test_agent_model_clone():
    """Test that model parameter is preserved when cloning."""
    agent = RealtimeAgent(name="test", instructions="Hello", model="gpt-4o-realtime-preview")

    # Clone with same model
    cloned = agent.clone()
    assert cloned.model == "gpt-4o-realtime-preview"

    # Clone with different model
    cloned_different = agent.clone(model="gpt-realtime")
    assert cloned_different.model == "gpt-realtime"
    assert agent.model == "gpt-4o-realtime-preview"  # Original unchanged

    # Clone removing model
    cloned_no_model = agent.clone(model=None)
    assert cloned_no_model.model is None
