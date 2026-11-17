import asyncio

from agents import Agent, Runner
from examples.auto_mode import input_with_fallback, is_auto_mode

"""This demonstrates usage of the `previous_response_id` parameter to continue a conversation.
The second run passes the previous response ID to the model, which allows it to continue the
conversation without re-sending the previous messages.

Notes:
1. This example requires stored response IDs from the OpenAI Responses API. For other providers,
use a session or pass result.to_input_list() to the next run.
2. Responses are only stored for 30 days as of this writing, so in production you should
store the response ID along with an expiration date; if the response is no longer valid,
you'll need to re-send the previous conversation history.
"""


async def main():
    print("=== Non-streaming Example ===")
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. be VERY concise.",
    )

    result = await Runner.run(
        agent, "What is the largest country in South America?", auto_previous_response_id=True
    )
    print(result.final_output)
    # Brazil

    result = await Runner.run(
        agent,
        "What is the capital of that country?",
        previous_response_id=result.last_response_id,
    )
    print(result.final_output)
    # Brasilia


async def main_stream():
    print("=== Streaming Example ===")
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. be VERY concise.",
    )

    result = Runner.run_streamed(
        agent, "What is the largest country in South America?", auto_previous_response_id=True
    )

    async for event in result.stream_events():
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta":
            print(event.data.delta, end="", flush=True)

    print()

    result = Runner.run_streamed(
        agent,
        "What is the capital of that country?",
        previous_response_id=result.last_response_id,
    )

    async for event in result.stream_events():
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta":
            print(event.data.delta, end="", flush=True)


async def main_auto():
    await main()
    print()
    await main_stream()


if __name__ == "__main__":
    if is_auto_mode():
        asyncio.run(main_auto())
    else:
        is_stream = input_with_fallback("Run in stream mode? (y/n): ", "n")
        if is_stream == "y":
            asyncio.run(main_stream())
        else:
            asyncio.run(main())
