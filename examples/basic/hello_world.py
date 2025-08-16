import asyncio

from agents import Agent, Runner
# NOTE: This requires the external module `agentsdk_gemini_adapter`.
# Make sure to set `GEMINI_API_KEY` in your `.env` file before using this configuration.
from agentsdk_gemini_adapter import config 


async def main():
    agent = Agent(
        name="GeminiHelper",
        instructions="You are a helpful AI assistant that explains concepts clearly with examples.",
    )

    result = await Runner.run(
        agent,
        "Explain the difference between synchronous and asynchronous programming with Python examples.",
        run_config=config,
    )

    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
