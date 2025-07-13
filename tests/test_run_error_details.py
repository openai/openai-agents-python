import json

import pytest

from agents import Agent, MaxTurnsExceeded, RunErrorDetails, Runner

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


@pytest.mark.asyncio
async def test_run_error_includes_data():
    model = FakeModel()
    agent = Agent(name="test", model=model, tools=[get_function_tool("foo", "res")])
    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_function_tool_call("foo", json.dumps({"a": "b"}))],
            [get_text_message("done")],
        ]
    )
    with pytest.raises(MaxTurnsExceeded) as exc:
        await Runner.run(agent, input="hello", max_turns=1)
    data = exc.value.run_data
    assert isinstance(data, RunErrorDetails)
    assert data.last_agent == agent
    assert len(data.raw_responses) == 1
    assert len(data.new_items) > 0


@pytest.mark.asyncio
async def test_streamed_run_error_includes_data():
    model = FakeModel()
    agent = Agent(name="test", model=model, tools=[get_function_tool("foo", "res")])
    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_function_tool_call("foo", json.dumps({"a": "b"}))],
            [get_text_message("done")],
        ]
    )
    result = Runner.run_streamed(agent, input="hello", max_turns=1)
    with pytest.raises(MaxTurnsExceeded) as exc:
        async for _ in result.stream_events():
            pass
    data = exc.value.run_data
    assert isinstance(data, RunErrorDetails)
    assert data.last_agent == agent
    assert len(data.raw_responses) == 1
    assert len(data.new_items) > 0
