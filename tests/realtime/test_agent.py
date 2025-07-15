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
