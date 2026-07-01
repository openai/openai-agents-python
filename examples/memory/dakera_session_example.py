"""Example demonstrating Dakera-backed session memory.

[Dakera](https://github.com/dakera-ai/dakera-deploy) is a self-hosted memory
server for AI agents. This example uses ``DakeraSession`` to persist conversation
history on a Dakera server so an agent keeps context across multiple runs.

Run a local Dakera server with the ``dakera-ai/dakera-deploy`` docker-compose
stack (Dakera server + MinIO); it listens on http://localhost:3000 by default.
Set ``DAKERA_BASE_URL`` and ``DAKERA_API_KEY`` to point at your own server.

    pip install "openai-agents[dakera]"
"""

import asyncio
import os

from agents import Agent, Runner
from agents.extensions.memory import DakeraSession

DEFAULT_BASE_URL = "http://localhost:3000"


async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    base_url = os.environ.get("DAKERA_BASE_URL", DEFAULT_BASE_URL)
    api_key = os.environ.get("DAKERA_API_KEY")

    print("=== Dakera Session Example ===")
    print(f"This example uses a Dakera server at {base_url}")
    print("Set DAKERA_BASE_URL / DAKERA_API_KEY to use a different server.\n")

    # `from_url` creates and owns the AsyncDakeraClient; `close()` releases it.
    session = DakeraSession.from_url(
        session_id="dakera_conversation_123",
        base_url=base_url,
        api_key=api_key,
    )

    try:
        # Clear any existing history for a clean demonstration.
        await session.clear_session()
        print("Session cleared for clean demonstration.")
        print("The agent will remember previous messages automatically.\n")

        print("First turn:")
        print("User: What city is the Golden Gate Bridge in?")
        result = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        print(f"Assistant: {result.final_output}\n")

        print("Second turn:")
        print("User: What state is it in?")
        result = await Runner.run(agent, "What state is it in?", session=session)
        print(f"Assistant: {result.final_output}\n")

        print("Third turn:")
        print("User: What's the population of that state?")
        result = await Runner.run(
            agent,
            "What's the population of that state?",
            session=session,
        )
        print(f"Assistant: {result.final_output}\n")

        print("=== Conversation Complete ===")
        all_items = await session.get_items()
        print(f"Total items stored in Dakera: {len(all_items)}")

        # Demonstrate the limit parameter.
        latest_items = await session.get_items(limit=2)
        print(f"Latest {len(latest_items)} items retrieved via the limit parameter.")

        # Demonstrate session isolation with a second conversation.
        other = DakeraSession.from_url(
            session_id="different_conversation_456",
            base_url=base_url,
            api_key=api_key,
        )
        try:
            await other.clear_session()
            await Runner.run(agent, "Hello, this is a new conversation!", session=other)
            print(
                "\nSession isolation: "
                f"original={len(await session.get_items())} items, "
                f"new={len(await other.get_items())} items"
            )
        finally:
            await other.close()

    except Exception as e:  # pragma: no cover - example error handling
        print(f"Error: {e}")
        print(f"Make sure a Dakera server is running and reachable at {base_url}.")
        print("See https://github.com/dakera-ai/dakera-deploy for a docker-compose setup.")
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
