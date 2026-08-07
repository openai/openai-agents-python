"""Use Azure Cosmos DB for durable Agents SDK session memory.

Prerequisites:

1. Install ``openai-agents[cosmosdb]``.
2. Create a Cosmos DB for NoSQL database and container partitioned by ``/sessionId``.
3. Set ``AZURE_COSMOS_CONNECTION_STRING`` and ``OPENAI_API_KEY``.

See ``docs/sessions/cosmosdb_session.md`` for the required indexing policy and
optional TTL configuration.
"""

import asyncio
import os

from agents import Agent, Runner
from agents.extensions.memory import CosmosDBSession


async def main() -> None:
    connection_string = os.environ.get("AZURE_COSMOS_CONNECTION_STRING")
    if not connection_string:
        raise RuntimeError("Set AZURE_COSMOS_CONNECTION_STRING before running this example")

    session = await CosmosDBSession.from_connection_string(
        os.environ.get("AZURE_COSMOS_SESSION_ID", "cosmosdb_conversation_123"),
        connection_string,
        database=os.environ.get("AZURE_COSMOS_DATABASE", "agents"),
        container=os.environ.get("AZURE_COSMOS_CONTAINER", "agent_sessions"),
    )
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    try:
        result = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        print(f"Assistant: {result.final_output}")

        result = await Runner.run(
            agent,
            "What state is it in?",
            session=session,
        )
        print(f"Assistant: {result.final_output}")
        print(f"Stored session items: {len(await session.get_items())}")
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
