from __future__ import annotations

from typing import Any, cast
from unittest import mock

import pytest

from agents import Agent, Runner
from agents.run import AgentRunner, set_default_agent_runner
from agents.testing import ScriptedModel

from .test_responses import get_text_input_item, get_text_message


@pytest.mark.asyncio
async def test_static_run_methods_call_into_default_runner() -> None:
    runner = mock.Mock(spec=AgentRunner)
    set_default_agent_runner(runner)

    agent = Agent(name="test", model=ScriptedModel())
    await Runner.run(agent, input="test")
    runner.run.assert_called_once()

    Runner.run_streamed(agent, input="test")
    runner.run_streamed.assert_called_once()

    Runner.run_sync(agent, input="test")
    runner.run_sync.assert_called_once()


@pytest.mark.asyncio
async def test_run_preserves_duplicate_user_messages() -> None:
    model = ScriptedModel()
    model.enqueue([get_text_message("done")])
    agent = Agent(name="test", model=model)

    input_items = [get_text_input_item("repeat"), get_text_input_item("repeat")]

    await Runner.run(agent, input=input_items)

    sent_input = model.calls[-1].input
    assert isinstance(sent_input, list)
    assert len(sent_input) == 2
    assert cast(dict[str, Any], sent_input[0])["content"] == "repeat"
    assert cast(dict[str, Any], sent_input[1])["content"] == "repeat"
