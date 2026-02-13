"""
Example demonstrating Redis session memory functionality.

This example shows how to use Redis-backed session memory to maintain conversation
history across multiple agent runs with persistence and scalability.

Note: This example clears the session at the start to ensure a clean demonstration.
In production, you may want to preserve existing conversation history.
"""

import asyncio

from agents import Agent, Runner
from agents.extensions.memory import RedisSession


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    print("=== Redis Session Example ===")
    print("This example requires Redis to be running on localhost:6379")
    print("Start Redis with: redis-server")
    print()

    # Create a Redis session instance
    session_id = "redis_conversation_123"
    try:
        session = RedisSession.from_url(
            session_id,
            url="redis://localhost:6379/0",  # Use database 0
        )

        # Test Redis connectivity
        if not await session.ping():
            print("Redis server is not available!")
            print("Please start Redis server and try again.")
            return

        print("Connected to Redis successfully!")
        print(f"Session ID: {session_id}")

        # Clear any existing session data for a clean start
        await session.clear_session()
        print("Session cleared for clean demonstration.")
        print("The agent will remember previous messages automatically.\n")

        # First turn
        print("First turn:")
        print("User: What city is the Golden Gate Bridge in?")
        result = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        print(f"Assistant: {result.final_output}")
        print()

        # Second turn - the agent will remember the previous conversation
        print("Second turn:")
        print("User: What state is it in?")
        result = await Runner.run(agent, "What state is it in?", session=session)
        print(f"Assistant: {result.final_output}")
        print()

        # Third turn - continuing the conversation
        print("Third turn:")
        print("User: What's the population of that state?")
        result = await Runner.run(
            agent,
            "What's the population of that state?",
            session=session,
        )
        print(f"Assistant: {result.final_output}")
        print()

        print("=== Conversation Complete ===")
        print("Notice how the agent remembered the context from previous turns!")
        print("Redis session automatically handles conversation history with persistence.")

        # Demonstrate session persistence
        print("\n=== Session Persistence Demo ===")
        all_items = await session.get_items()
        print(f"Total messages stored in Redis: {len(all_items)}")

        # Demonstrate the limit parameter
        print("\n=== Latest Items Demo ===")
        latest_items = await session.get_items(limit=2)
        print("Latest 2 items:")
        for i, msg in enumerate(latest_items, 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            print(f"  {i}. {role}: {content}")

        # Demonstrate session isolation with a new session
        print("\n=== Session Isolation Demo ===")
        new_session = RedisSession.from_url(
            "different_conversation_456",
            url="redis://localhost:6379/0",
        )

        print("Creating a new session with different ID...")
        result = await Runner.run(
            agent,
            "Hello, this is a new conversation!",
            session=new_session,
        )
        print(f"New session response: {result.final_output}")

        # Show that sessions are isolated
        original_items = await session.get_items()
        new_items = await new_session.get_items()
        print(f"Original session has {len(original_items)} items")
        print(f"New session has {len(new_items)} items")
        print("Sessions are completely isolated!")

        # Clean up the new session
        await new_session.clear_session()
        await new_session.close()

        # Optional: Demonstrate TTL (time-to-live) functionality
        print("\n=== TTL Demo ===")
        ttl_session = RedisSession.from_url(
            "ttl_demo_session",
            url="redis://localhost:6379/0",
            ttl=3600,  # 1 hour TTL
        )

        await Runner.run(
            agent,
            "This message will expire in 1 hour",
            session=ttl_session,
        )
        print("Created session with 1-hour TTL - messages will auto-expire")

        await ttl_session.close()

        # Close the main session
        await session.close()

    except Exception as e:
        print(f"Error: {e}")
        print("Make sure Redis is running on localhost:6379")


async def demonstrate_advanced_features():
    """Demonstrate advanced Redis session features."""
    print("\n=== Advanced Features Demo ===")

    # Custom key prefix for multi-tenancy
    tenant_session = RedisSession.from_url(
        "user_123",
        url="redis://localhost:6379/0",
        key_prefix="tenant_abc:sessions",  # Custom prefix for isolation
    )

    try:
        if await tenant_session.ping():
            print("Custom key prefix demo:")
            await Runner.run(
                Agent(name="Support", instructions="Be helpful"),
                "Hello from tenant ABC",
                session=tenant_session,
            )
            print("Session with custom key prefix created successfully")

        await tenant_session.close()
    except Exception as e:
        print(f"Advanced features error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(demonstrate_advanced_features())
