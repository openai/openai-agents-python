import asyncio
import os

from openai import AsyncOpenAI

from agents import (
    Agent,
    OpenAIChatCompletionsModel,
    Runner,
    set_tracing_disabled,
)
from agents.decorators import tool

"""This example uses OrcaRouter as a named provider for a specific agent.

OrcaRouter (https://www.orcarouter.ai) is an OpenAI-compatible AI gateway. Like
OpenRouter, it exposes a single base URL and a provider/model namespace across many
models, but it also adds adaptive routing, automatic failover, and gateway-level
security for AI agents on the same endpoint. We point the Chat Completions model at
the OrcaRouter base URL and use the dedicated ORCAROUTER_API_KEY, mirroring how the
OpenRouter examples in this repository are wired up.

Note that in this example, we disable tracing under the assumption that you don't have
an API key from platform.openai.com. If you do have one, you can either set the
`OPENAI_API_KEY` env var or call set_tracing_export_api_key() to set a tracing specific
key.
"""
API_KEY = os.getenv("ORCAROUTER_API_KEY") or ""
MODEL_NAME = os.getenv("ORCAROUTER_MODEL") or "gpt-5.6-luna"

if not API_KEY:
    raise ValueError("Please set ORCAROUTER_API_KEY via env var or code.")

client = AsyncOpenAI(base_url="https://api.orcarouter.ai/v1", api_key=API_KEY)
set_tracing_disabled(disabled=True)

# An alternate approach that would also work:
# PROVIDER = OpenAIProvider(openai_client=client)
# agent = Agent(..., model="some-custom-model")
# Runner.run(agent, ..., run_config=RunConfig(model_provider=PROVIDER))


@tool
def get_weather(city: str):
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main():
    # This agent will use OrcaRouter for its model calls.
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model=OpenAIChatCompletionsModel(model=MODEL_NAME, openai_client=client),
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
