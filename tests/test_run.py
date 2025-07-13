from __future__ import annotations

from unittest import mock

import pytest

from agents import Agent, Runner
from agents.run import AgentRunner, set_default_agent_runner

from .fake_model import FakeModel


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
