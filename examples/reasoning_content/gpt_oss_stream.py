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

# import logging
# logging.basicConfig(level=logging.DEBUG)


async def main():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("Set OPENROUTER_API_KEY before running the OpenRouter example.")

    set_tracing_disabled(True)
    # Passing this model object has been bypassing the default provider, including CodexProvider.
    gpt_oss_model = OpenAIChatCompletionsModel(
        model="openai/gpt-oss-20b",
        openai_client=AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        ),
    )
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
