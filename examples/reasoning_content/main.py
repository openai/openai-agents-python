"""
Example demonstrating how to access reasoning summaries when a model returns them.

Some models, like gpt-5.6, provide reasoning summaries in addition to the regular content.
This example shows how to access that content from both streaming and non-streaming responses,
and verifies that the requested summary was returned.

To run this example, you need to:
1. Set your OPENAI_API_KEY environment variable
2. Use a model that supports reasoning summaries (e.g., gpt-5.6)
"""

import asyncio
import os

from openai.types.responses import (
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseReasoningItem,
)
from openai.types.shared.reasoning import Reasoning

from agents import ModelSettings
from agents.models.interface import ModelTracing
from agents.models.openai_provider import OpenAIProvider

MODEL_NAME = os.getenv("REASONING_MODEL_NAME") or "gpt-5.6"


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

    output_text_already_started = False
    async for event in model.stream_response(
        system_instructions="You are a helpful assistant that writes creative content.",
        input="Write a haiku about recursion in programming",
        model_settings=ModelSettings(reasoning=Reasoning(effort="high", summary="auto")),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        if event.type == "response.reasoning_summary_text.delta":
            # Yellow for reasoning content
            print(f"\033[33m{event.delta}\033[0m", end="", flush=True)
            reasoning_content += event.delta
        elif event.type == "response.output_text.delta":
            if not output_text_already_started:
                print("\n")
                output_text_already_started = True
            # Green for regular content
            print(f"\033[32m{event.delta}\033[0m", end="", flush=True)
            regular_content += event.delta
    if not reasoning_content:
        raise RuntimeError(f"Model {MODEL_NAME} returned no reasoning summary deltas.")
    if not regular_content:
        raise RuntimeError(f"Model {MODEL_NAME} returned no output text deltas.")
    print("\n")


async def get_response_with_reasoning_content():
    """
    Example of getting a complete response from a model that provides reasoning content.
    The reasoning content will be available as a separate item in the response.
    """
    provider = OpenAIProvider()
    model = provider.get_model(MODEL_NAME)

    print("\n=== Non-streaming Example ===")
    prompt = (
        "A recursive function uses T(n) = 2 * T(n - 1) + 1 with T(0) = 1. "
        "Compute T(20) and derive a closed form."
    )
    print(f"Prompt: {prompt}")

    response = await model.get_response(
        system_instructions="You are a helpful assistant that explains technical concepts clearly.",
        input=prompt,
        model_settings=ModelSettings(reasoning=Reasoning(effort="high", summary="auto")),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    # Extract reasoning content and regular content from the response
    reasoning_parts: list[str] = []
    regular_parts: list[str] = []

    for item in response.output:
        if isinstance(item, ResponseReasoningItem):
            reasoning_parts.extend(summary.text for summary in item.summary)
        elif isinstance(item, ResponseOutputMessage):
            for content_item in item.content:
                if isinstance(content_item, ResponseOutputText):
                    regular_parts.append(content_item.text)
                elif isinstance(content_item, ResponseOutputRefusal):
                    regular_parts.append(content_item.refusal)

    reasoning_content = "\n".join(reasoning_parts)
    regular_content = "\n".join(regular_parts)

    if not reasoning_content:
        raise RuntimeError(f"Model {MODEL_NAME} returned no reasoning summary.")
    if not regular_content:
        raise RuntimeError(f"Model {MODEL_NAME} returned no regular output content.")

    print("\n\n### Reasoning Content:")
    print(reasoning_content)
    print("\n\n### Regular Content:")
    print(regular_content)
    print("\n")


async def main():
    await stream_with_reasoning_content()
    await get_response_with_reasoning_content()


if __name__ == "__main__":
    asyncio.run(main())
