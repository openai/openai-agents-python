from __future__ import annotations

import asyncio
import os

from agents import Agent, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.any_llm_model import AnyLLMModel

"""This example uses the AnyLLMModel directly.

You can run it like this:
uv run examples/model_providers/any_llm_provider.py --model openrouter/openai/gpt-5.4-mini
or
uv run examples/model_providers/any_llm_provider.py --model openrouter/anthropic/claude-4.5-sonnet
"""

set_tracing_disabled(disabled=True)


@function_tool
def get_weather(city: str):
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main(model: str, api_key: str):
    if api_key == "dummy":
        print("Skipping run because no valid OPENROUTER_API_KEY was provided.")
        return

    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model=AnyLLMModel(model=model, api_key=api_key),
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, required=False)
    parser.add_argument("--api-key", type=str, required=False)
    args = parser.parse_args()

    model = args.model or os.environ.get("ANY_LLM_MODEL", "openrouter/openai/gpt-5.4-mini")
    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY", "dummy")

    if not args.model:
        print(f"Using default model: {model}")
    if not args.api_key:
        print("Using OPENROUTER_API_KEY from environment (or dummy placeholder).")

    asyncio.run(main(model, api_key))
