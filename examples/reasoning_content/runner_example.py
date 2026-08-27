"""
Example demonstrating how to use the reasoning content feature with the Runner API.

This example shows how to extract and use reasoning content from responses when using
the Runner API, which is the most common way users interact with the Agents library.

To run this example, you need to:
1. Set your OPENAI_API_KEY environment variable
2. Use a model that supports reasoning summaries (e.g., gpt-5.6)
"""

import asyncio
import os

from openai.types.shared.reasoning import Reasoning

from agents import Agent, ModelSettings, Runner, trace
from agents.items import ReasoningItem

MODEL_NAME = os.getenv("REASONING_MODEL_NAME") or "gpt-5.6"


async def main():
    print(f"Using model: {MODEL_NAME}")

    # Create an agent with a model that supports reasoning content
    agent = Agent(
        name="Reasoning Agent",
        instructions="You are a helpful assistant that explains your reasoning step by step.",
        model=MODEL_NAME,
        model_settings=ModelSettings(reasoning=Reasoning(effort="high", summary="auto")),
    )

    # Example 1: Non-streaming response
    with trace("Reasoning Content - Non-streaming"):
        print("\n=== Example 1: Non-streaming response ===")
        result = await Runner.run(
            agent, "What is the square root of 841? Please explain your reasoning."
        )
        # Extract reasoning content from the result items
        reasoning_parts: list[str] = []
        for item in result.new_items:
            if isinstance(item, ReasoningItem):
                reasoning_parts.extend(summary.text for summary in item.raw_item.summary)

        reasoning_content = "\n".join(reasoning_parts)

        if not reasoning_content:
            raise RuntimeError(f"Model {MODEL_NAME} returned no reasoning summary.")

        print("\n### Reasoning Content:")
        print(reasoning_content)
        print("\n### Final Output:")
        print(result.final_output)

    # Example 2: Streaming response
    with trace("Reasoning Content - Streaming"):
        print("\n=== Example 2: Streaming response ===")
        stream = Runner.run_streamed(
            agent,
            "A recursive function uses T(n) = 2 * T(n - 1) + 1 with T(0) = 1. "
            "Compute T(20) and derive a closed form.",
        )
        output_text_already_started = False
        saw_reasoning_summary_delta = False
        saw_output_text_delta = False
        async for event in stream.stream_events():
            if event.type == "raw_response_event":
                if event.data.type == "response.reasoning_summary_text.delta":
                    saw_reasoning_summary_delta = True
                    print(f"\033[33m{event.data.delta}\033[0m", end="", flush=True)
                elif event.data.type == "response.output_text.delta":
                    saw_output_text_delta = True
                    if not output_text_already_started:
                        print("\n")
                        output_text_already_started = True
                    print(f"\033[32m{event.data.delta}\033[0m", end="", flush=True)

        if not saw_reasoning_summary_delta:
            raise RuntimeError(f"Model {MODEL_NAME} returned no streaming reasoning summary.")
        if not saw_output_text_delta:
            raise RuntimeError(f"Model {MODEL_NAME} returned no streaming output text.")
        print("\n")


if __name__ == "__main__":
    asyncio.run(main())
