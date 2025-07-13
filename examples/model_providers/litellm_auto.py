from __future__ import annotations

import asyncio

from agents import Agent, Runner, function_tool, set_tracing_disabled

"""This example uses the built-in support for LiteLLM. To use this, ensure you have the
ANTHROPIC_API_KEY environment variable set.
"""

set_tracing_disabled(disabled=True)


@function_tool
def get_weather(city: str):
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main():
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        # We prefix with litellm/ to tell the Runner to use the LitellmModel
        model="litellm/anthropic/claude-3-5-sonnet-20240620",
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)


if __name__ == "__main__":
    import os

    if os.getenv("ANTHROPIC_API_KEY") is None:
        raise ValueError(
            "ANTHROPIC_API_KEY is not set. Please set it the environment variable and try again."
        )

    asyncio.run(main())
