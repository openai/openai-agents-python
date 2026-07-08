"""
Example demonstrating pop_item for correcting the most recent conversation turn.

Use pop_item when a user wants to undo or replace their last message without
starting a new session.
"""

import asyncio

from agents import Agent, Runner, SQLiteSession


async def main():
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely with just the numeric answer.",
    )

    session = SQLiteSession("session_pop_item_demo")

    print("=== Session pop_item Example ===\n")

    print("Turn 1:")
    print("User: What's 2 + 2?")
    result = await Runner.run(agent, "What's 2 + 2?", session=session)
    print(f"Assistant: {result.final_output}\n")

    print("User wants to correct the question instead of adding a new turn.")
    await session.pop_item()  # Remove the assistant reply
    await session.pop_item()  # Remove the original user question

    print("Turn 1 (corrected):")
    print("User: What's 2 + 3?")
    result = await Runner.run(agent, "What's 2 + 3?", session=session)
    print(f"Assistant: {result.final_output}\n")

    items = await session.get_items()
    print(f"Session now has {len(items)} items (one corrected question and answer).")


if __name__ == "__main__":
    asyncio.run(main())
