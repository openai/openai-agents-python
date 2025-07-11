"""
Example demonstrating how to use the reasoning content feature with the Runner API.

This example shows how to extract and use reasoning content from responses when using
the Runner API, which is the most common way users interact with the Agents library.

To run this example, you need to:
1. Set your OPENAI_API_KEY environment variable
2. Use a model that supports reasoning content (e.g., deepseek-reasoner)
"""

import asyncio
import os
from typing import Any

from agents import Agent, Runner, trace
from agents.items import ReasoningItem

MODEL_NAME = os.getenv("EXAMPLE_MODEL_NAME") or "deepseek-reasoner"


async def main():
    print(f"Using model: {MODEL_NAME}")

    # Create an agent with a model that supports reasoning content
    agent = Agent(
        name="Reasoning Agent",
        instructions="You are a helpful assistant that explains your reasoning step by step.",
        model=MODEL_NAME,
    )

    # Example 1: Non-streaming response
    with trace("Reasoning Content - Non-streaming"):
        print("\n=== Example 1: Non-streaming response ===")
        result = await Runner.run(
            agent, "What is the square root of 841? Please explain your reasoning."
        )

        # Extract reasoning content from the result items
        reasoning_content = None
        # RunResult has 'response' attribute which has 'output' attribute
        for item in result.response.output:  # type: ignore
            if isinstance(item, ReasoningItem):
                reasoning_content = item.summary[0].text  # type: ignore
                break

        print("\nReasoning Content:")
        print(reasoning_content or "No reasoning content provided")

        print("\nFinal Output:")
        print(result.final_output)

    # Example 2: Streaming response
    with trace("Reasoning Content - Streaming"):
        print("\n=== Example 2: Streaming response ===")
        print("\nStreaming response:")

        # Buffers to collect reasoning and regular content
        reasoning_buffer = ""
        content_buffer = ""

        # RunResultStreaming is async iterable
        stream = Runner.run_streamed(agent, "What is 15 x 27? Please explain your reasoning.")

        async for event in stream:  # type: ignore
            if isinstance(event, ReasoningItem):
                # This is reasoning content
                reasoning_item: Any = event
                reasoning_buffer += reasoning_item.summary[0].text
                print(
                    f"\033[33m{reasoning_item.summary[0].text}\033[0m", end="", flush=True
                )  # Yellow for reasoning
            elif hasattr(event, "text"):
                # This is regular content
                content_buffer += event.text
                print(
                    f"\033[32m{event.text}\033[0m", end="", flush=True
                )  # Green for regular content

        print("\n\nCollected Reasoning Content:")
        print(reasoning_buffer)

        print("\nCollected Final Answer:")
        print(content_buffer)


if __name__ == "__main__":
    asyncio.run(main())
