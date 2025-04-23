import pytest

from agents import Agent, Runner

from .fake_model import FakeModel


@pytest.mark.asyncio
async def test_joker_streamed_jokes_with_cancel():
    model = FakeModel()
    agent = Agent(name="Joker", model=model)

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    num_events = 0
    stop_after = 1  # There are two that the model gives back.

    async for _event in result.stream_events():
        num_events += 1
        if num_events == 1:
            result.cancel()

    assert num_events == 1, f"Expected {stop_after} visible events, but got {num_events}"
