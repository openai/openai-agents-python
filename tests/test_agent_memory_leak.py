from __future__ import annotations

import gc
import weakref

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents import Agent, Runner
from agents.testing import ScriptedModel


def _make_message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="msg-1",
        content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
        role="assistant",
        status="completed",
        type="message",
    )


@pytest.mark.asyncio
async def test_agent_is_released_after_run() -> None:
    scripted_model = ScriptedModel(steps=[[_make_message("Paris")]])
    agent = Agent(name="leak-test-agent", instructions="Answer questions.", model=scripted_model)
    agent_ref = weakref.ref(agent)

    # Running the agent should not leave behind strong references once the result goes out of scope.
    await Runner.run(agent, "What is the capital of France?")

    del agent
    gc.collect()

    assert agent_ref() is None
