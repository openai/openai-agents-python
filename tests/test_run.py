from __future__ import annotations

from unittest import mock

import pytest

from agents import Agent, AgentRunner, Runner, run as run_module

from .fake_model import FakeModel


@pytest.mark.asyncio
async def test_static_run_methods_call_into_default_runner() -> None:
    runner = mock.Mock(spec=AgentRunner)
    run_module.DEFAULT_RUNNER = runner

    agent = Agent(name="test", model=FakeModel())
    await Runner.run(agent, input="test")
    runner.run_impl.assert_called_once()

    Runner.run_streamed(agent, input="test")
    runner.run_streamed_impl.assert_called_once()

    Runner.run_sync(agent, input="test")
    runner.run_sync_impl.assert_called_once()
