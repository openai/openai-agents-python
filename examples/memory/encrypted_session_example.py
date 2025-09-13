"""
Example demonstrating encrypted session memory functionality.

This example shows how to use encrypted session memory to maintain conversation history
across multiple agent runs with automatic encryption and TTL-based expiration.
The EncryptedSession wrapper provides transparent encryption over any underlying session.
"""

import asyncio
from typing import cast

from agents import Agent, Runner, SQLiteSession
from agents.extensions.memory import EncryptedSession
from agents.extensions.memory.encrypt_session import EncryptedEnvelope


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    # Create an underlying session (SQLiteSession in this example)
    session_id = "conversation_123"
    underlying_session = SQLiteSession(session_id)

    # Wrap with encrypted session for automatic encryption and TTL
    session = EncryptedSession(
        session_id=session_id,
        underlying_session=underlying_session,
        encryption_key="my-secret-encryption-key",
        ttl=3600,  # 1 hour TTL for messages
    )

    print("=== Encrypted Session Example ===")
    print("The agent will remember previous messages automatically with encryption.\n")

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
    print("All conversation history was automatically encrypted and stored securely.")

    # Demonstrate the limit parameter - get only the latest 2 items
    print("\n=== Latest Items Demo ===")
    latest_items = await session.get_items(limit=2)
    print("Latest 2 items (automatically decrypted):")
    for i, msg in enumerate(latest_items, 1):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        print(f"  {i}. {role}: {content}")

    print(f"\nFetched {len(latest_items)} out of total conversation history.")

    # Get all items to show the difference
    all_items = await session.get_items()
    print(f"Total items in session: {len(all_items)}")

    # Show that underlying storage is encrypted
    print("\n=== Encryption Demo ===")
    print("Checking underlying storage to verify encryption...")
    raw_items = await underlying_session.get_items()
    print("Raw encrypted items in underlying storage:")
    for i, item in enumerate(raw_items, 1):
        if isinstance(item, dict) and item.get("__enc__") == 1:
            enc_item = cast(EncryptedEnvelope, item)
            print(
                f"  {i}. Encrypted envelope: __enc__={enc_item['__enc__']}, "
                f"payload length={len(enc_item['payload'])}"
            )
        else:
            print(f"  {i}. Unencrypted item: {item}")

    print(f"\nAll {len(raw_items)} items are stored encrypted with TTL-based expiration.")

    # Clean up
    underlying_session.close()


if __name__ == "__main__":
    asyncio.run(main())
