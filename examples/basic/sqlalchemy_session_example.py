import asyncio

from agents import Agent, Runner
from agents.extensions.memory.sqlalchemy_session import SQLAlchemySession


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    # Create a session instance with a session ID.
    # This example uses an in-memory SQLite database.
    # The `create_tables=True` flag is useful for development and testing.
    session = SQLAlchemySession.from_url(
        "conversation_123",
        url="sqlite+aiosqlite:///:memory:",
        create_tables=True,
    )

    print("=== SQLAlchemySession Example ===")
    print("The agent will remember previous messages automatically.\n")

    # First turn
    print("User: What city is the Golden Gate Bridge in?")
    result = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    print(f"Assistant: {result.final_output}\n")

    # Second turn - the agent will remember the previous conversation
    print("User: What state is it in?")
    result = await Runner.run(
        agent,
        "What state is it in?",
        session=session,
    )
    print(f"Assistant: {result.final_output}\n")

    print("=== Conversation Complete ===")


if __name__ == "__main__":
    # To run this example, you need to install the sqlalchemy extras:
    # pip install "agents[sqlalchemy]"
    asyncio.run(main())
