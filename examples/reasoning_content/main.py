"""
Example demonstrating how to use the reasoning content feature with models that support it.

Some models, like deepseek-reasoner, provide a reasoning_content field in addition to the regular content.
This example shows how to access and use this reasoning content from both streaming and non-streaming responses.

To run this example, you need to:
1. Set your OPENAI_API_KEY environment variable
2. Use a model that supports reasoning content (e.g., deepseek-reasoner)
"""

import asyncio
import os
from typing import Any, cast

from openai.types.responses import ResponseOutputRefusal, ResponseOutputText

from agents import ModelSettings
from agents.models.interface import ModelTracing
from agents.models.openai_provider import OpenAIProvider

MODEL_NAME = os.getenv("EXAMPLE_MODEL_NAME") or "deepseek-reasoner"


async def stream_with_reasoning_content():
    """
    Example of streaming a response from a model that provides reasoning content.
    The reasoning content will be emitted as separate events.
    """
    provider = OpenAIProvider()
    model = provider.get_model(MODEL_NAME)

    print("\n=== Streaming Example ===")
    print("Prompt: Write a haiku about recursion in programming")

    reasoning_content = ""
    regular_content = ""

    async for event in model.stream_response(
        system_instructions="You are a helpful assistant that writes creative content.",
        input="Write a haiku about recursion in programming",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        prompt=None,
    ):
        if event.type == "response.reasoning_summary_text.delta":
            print(
                f"\033[33m{event.delta}\033[0m", end="", flush=True
            )  # Yellow for reasoning content
            reasoning_content += event.delta
        elif event.type == "response.output_text.delta":
            print(f"\033[32m{event.delta}\033[0m", end="", flush=True)  # Green for regular content
            regular_content += event.delta

    print("\n\nReasoning Content:")
    print(reasoning_content)
    print("\nRegular Content:")
    print(regular_content)
    print("\n")


async def get_response_with_reasoning_content():
    """
    Example of getting a complete response from a model that provides reasoning content.
    The reasoning content will be available as a separate item in the response.
    """
    provider = OpenAIProvider()
    model = provider.get_model(MODEL_NAME)

    print("\n=== Non-streaming Example ===")
    print("Prompt: Explain the concept of recursion in programming")

    response = await model.get_response(
        system_instructions="You are a helpful assistant that explains technical concepts clearly.",
        input="Explain the concept of recursion in programming",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        prompt=None,
    )

    # Extract reasoning content and regular content from the response
    reasoning_content = None
    regular_content = None

    for item in response.output:
        if hasattr(item, "type") and item.type == "reasoning":
            reasoning_content = item.summary[0].text
        elif hasattr(item, "type") and item.type == "message":
            if item.content and len(item.content) > 0:
                content_item = item.content[0]
                if isinstance(content_item, ResponseOutputText):
                    regular_content = content_item.text
                elif isinstance(content_item, ResponseOutputRefusal):
                    refusal_item = cast(Any, content_item)
                    regular_content = refusal_item.refusal

    print("\nReasoning Content:")
    print(reasoning_content or "No reasoning content provided")

    print("\nRegular Content:")
    print(regular_content or "No regular content provided")

    print("\n")


async def main():
    try:
        await stream_with_reasoning_content()
        await get_response_with_reasoning_content()
    except Exception as e:
        print(f"Error: {e}")
        print("\nNote: This example requires a model that supports reasoning content.")
        print("You may need to use a specific model like deepseek-reasoner or similar.")


if __name__ == "__main__":
    asyncio.run(main())
