from __future__ import annotations

import asyncio
import os

from openai import AsyncOpenAI

from agents import Agent, OpenAIChatCompletionsModel, Runner, set_tracing_disabled

TOKENLAB_API_KEY = os.getenv("TOKENLAB_API_KEY")
TOKENLAB_BASE_URL = os.getenv("TOKENLAB_BASE_URL", "https://api.tokenlab.sh/v1")
TOKENLAB_MODEL = os.getenv("TOKENLAB_MODEL", "gpt-5.4-mini")

if not TOKENLAB_API_KEY:
    raise ValueError("Please set TOKENLAB_API_KEY.")


client = AsyncOpenAI(base_url=TOKENLAB_BASE_URL, api_key=TOKENLAB_API_KEY)

# Disable OpenAI tracing when running only with a TokenLab API key.
set_tracing_disabled(disabled=True)


async def main():
    agent = Agent(
        name="Assistant",
        instructions="You are concise and practical.",
        model=OpenAIChatCompletionsModel(model=TOKENLAB_MODEL, openai_client=client),
    )

    result = await Runner.run(agent, "Explain why custom model providers are useful.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
