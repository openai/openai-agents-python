import os
import asyncio
from openai import AsyncOpenAI

from agents import Agent, Runner
from agents.models.openai_responses import OpenAIResponsesModel


client = AsyncOpenAI(base_url="http://localhost:8080/v1", api_key=os.getenv("OPENAI_API_KEY"), default_headers={'x-model-provider': 'openai'})
async def main():
    agent = Agent(
        name="Assistant",
        instructions="You are a humorous poet who can write funny poems of 4 lines.",
        model=OpenAIResponsesModel(model="gpt-4o-mini", openai_client=client)
    )

    result = await Runner.run(agent, "Write a poem on Masaic.")
    print(result.final_output)
    # Function calls itself,
    # Looping in smaller pieces,
    # Endless by design.


if __name__ == "__main__":
    asyncio.run(main())
