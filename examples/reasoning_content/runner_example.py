"""
Example demonstrating how to use the reasoning content feature with the Runner API.

This example shows how to extract and use reasoning content from responses when using
the Runner API, which is the most common way users interact with the Agents library.

To run this example, you need to:
1. Set your OPENAI_API_KEY environment variable
2. Use a model that supports reasoning content (e.g., gpt-5)
"""

import asyncio
import os

from openai.types.shared.reasoning import Reasoning

from agents import Agent, ModelSettings, Runner, trace
from agents.items import ReasoningItem

MODEL_NAME = os.getenv("EXAMPLE_MODEL_NAME") or "gpt-5"


async def main():
    print(f"Using model: {MODEL_NAME}")

    # Create an agent with a model that supports reasoning content
    agent = Agent(
        name="Reasoning Agent",
        instructions="You are a helpful assistant that explains your reasoning step by step.",
        model=MODEL_NAME,
        model_settings=ModelSettings(reasoning=Reasoning(effort="medium", summary="detailed")),
    )

    # Example 1: Non-streaming response
    with trace("Reasoning Content - Non-streaming"):
        print("\n=== Example 1: Non-streaming response ===")
        result = await Runner.run(
            agent, "What is the square root of 841? Please explain your reasoning."
        )
        # Extract reasoning content from the result items
        reasoning_content = None
        for item in result.new_items:
            if isinstance(item, ReasoningItem) and len(item.raw_item.summary) > 0:
                reasoning_content = item.raw_item.summary[0].text
                break

        print("\n### Reasoning Content:")
        print(reasoning_content or "No reasoning content provided")
        print("\n### Final Output:")
        print(result.final_output)

    # Example 2: Streaming response
    with trace("Reasoning Content - Streaming"):
        print("\n=== Example 2: Streaming response ===")
        stream = Runner.run_streamed(agent, "What is 15 x 27? Please explain your reasoning.")
        output_text_already_started = False
        async for event in stream.stream_events():
            if event.type == "raw_response_event":
                if event.data.type == "response.reasoning_summary_text.delta":
                    print(f"\033[33m{event.data.delta}\033[0m", end="", flush=True)
                elif event.data.type == "response.output_text.delta":
                    if not output_text_already_started:
                        print("\n")
                        output_text_already_started = True
                    print(f"\033[32m{event.data.delta}\033[0m", end="", flush=True)

        print("\n")


if __name__ == "__main__":
    asyncio.run(main())
