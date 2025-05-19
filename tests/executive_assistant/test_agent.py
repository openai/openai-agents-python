from __future__ import annotations

import pytest

from agents import Agent, Runner
from agents.agent import ToolsToFinalOutputResult
from agents.executive_assistant import executive_assistant_agent
from tests.fake_model import FakeModel


@pytest.mark.asyncio
async def test_agent_runs_with_fake_model() -> None:
    model = FakeModel()
    agent = Agent(
        name=executive_assistant_agent.name,
        instructions=executive_assistant_agent.instructions,
        tools=executive_assistant_agent.tools,
        model=model,
    )
    model.set_next_output(
        [
            {"role": "assistant", "content": "Hello"},
        ]
    )

    result: ToolsToFinalOutputResult = await Runner.run(agent, "hi")
    assert result.final_output == "Hello"
