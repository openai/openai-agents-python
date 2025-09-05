import asyncio
import os

from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    Runner,
    set_tracing_disabled,
)

set_tracing_disabled(True)

# import logging
# logging.basicConfig(level=logging.DEBUG)

gpt_oss_model = OpenAIChatCompletionsModel(
    model="openai/gpt-oss-20b",
    openai_client=AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
    ),
)


async def main():
    agent = Agent(
        name="Assistant",
        instructions="You're a helpful assistant. You provide a concise answer to the user's question.",
        model=gpt_oss_model,
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="high", summary="detailed"),
        ),
    )

    result = Runner.run_streamed(agent, "Tell me about recursion in programming.")
    print("=== Run starting ===")
    print("\n")
    async for event in result.stream_events():
        if event.type == "raw_response_event":
            if event.data.type == "response.reasoning_text.delta":
                print(f"\033[33m{event.data.delta}\033[0m", end="", flush=True)
            elif event.data.type == "response.output_text.delta":
                print(f"\033[32m{event.data.delta}\033[0m", end="", flush=True)

    print("\n")
    print("=== Run complete ===")


if __name__ == "__main__":
    asyncio.run(main())
