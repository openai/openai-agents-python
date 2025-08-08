from agents import Agent, Runner,set_tracing_disabled,OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from pg_session import PostgreSQLSession
from dotenv import load_dotenv
import asyncio
import os

load_dotenv(override=True)
set_tracing_disabled(disabled=True)
openai_client = AsyncOpenAI(
    api_key=os.getenv("OPEN_API_KEY",""),
    base_url="https://api.openai.com/v1",
)

async def main():
    # Create agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
        model=OpenAIChatCompletionsModel(
            model="gpt-4o-mini",
            openai_client=openai_client,
        )
    )

    # Create a session instance with a session ID
    session = PostgreSQLSession("conversation_123",neon_url=os.getenv("NEON_DB_URL"))

    # await session.clear_session()

    result = await Runner.run(
        agent,
        "what is my name",
        session=session
    )
    print(result.final_output)

asyncio.run(main())