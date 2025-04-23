import pytest
from openai.types.responses import ResponseTextDeltaEvent
from agents import Agent, Runner


@pytest.mark.asyncio
async def test_joker_streamed_jokes_with_cancel():
    agent = Agent(
        name="Joker",
        instructions="You are a helpful assistant.",
    )

    result = Runner.run_streamed(agent, input="Please tell me 5 jokes.")
    num_visible_event = 0
    
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            num_visible_event += 1
            if num_visible_event == 3:
                result.cancel()
    
    assert num_visible_event == 3, f"Expected 3 visible events, but got {num_visible_event}"
