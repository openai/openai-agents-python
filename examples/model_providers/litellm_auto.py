from __future__ import annotations

import asyncio

from pydantic import BaseModel

from agents import Agent, ModelSettings, Runner, function_tool, set_tracing_disabled

"""This example uses the built-in support for LiteLLM through OpenRouter.

Set OPENROUTER_API_KEY before running it.
"""

set_tracing_disabled(disabled=True)

# import logging
# logging.basicConfig(level=logging.DEBUG)


@function_tool
def get_weather(city: str):
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


class Result(BaseModel):
    output_text: str
    tool_results: list[str]


async def main():
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        # We prefix with litellm/ to tell the Runner to use the LitellmModel
        model="litellm/openrouter/openai/gpt-5.4-mini",
        tools=[get_weather],
        model_settings=ModelSettings(tool_choice="required"),
        output_type=Result,
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)


if __name__ == "__main__":
    import os

    if os.getenv("OPENROUTER_API_KEY") is None:
        raise ValueError(
            "OPENROUTER_API_KEY is not set. Please set the environment variable and try again."
        )

    asyncio.run(main())
