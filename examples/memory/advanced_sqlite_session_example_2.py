"""
Advanced example demonstrating conversation branching with AdvancedSQLiteSession.

This example shows how to use soft deletion for conversation editing/branching,
allowing you to "undo" parts of a conversation and continue from any point.
"""

import asyncio

from agents import Agent, Runner, function_tool
from agents.extensions.memory import AdvancedSQLiteSession


@function_tool
async def get_weather(city: str) -> str:
    if city.strip().lower() == "new york":
        return f"The weather in {city} is cloudy."
    return f"The weather in {city} is sunny."


async def main():
    # Create an agent
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
        tools=[get_weather],
    )

    # Create a advanced session instance
    session = AdvancedSQLiteSession(session_id="conversation_advanced")

    print("=== Advanced Session: Conversation Branching ===")
    print("This example demonstrates conversation editing and branching.\n")

    # Build initial conversation
    print("Building initial conversation...")

    # Turn 1
    print("Turn 1: User asks about Golden Gate Bridge")
    result = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    await session.store_run_usage(result)

    # Turn 2
    print("Turn 2: User asks about weather")
    result = await Runner.run(
        agent,
        "What's the weather in that city?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    await session.store_run_usage(result)

    # Turn 3
    print("Turn 3: User asks about population")
    result = await Runner.run(
        agent,
        "What's the population of that city?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    await session.store_run_usage(result)

    print("\n=== Original Conversation Complete ===")

    # Show current conversation
    print("Current conversation:")
    current_items = await session.get_items()
    for i, item in enumerate(current_items, 1):
        role = str(item.get("role", item.get("type", "unknown")))
        if item.get("type") == "function_call":
            content = f"{item.get('name', 'unknown')}({item.get('arguments', '{}')})"
        elif item.get("type") == "function_call_output":
            content = str(item.get("output", ""))
        else:
            content = str(item.get("content", item.get("output", "")))
        print(f"  {i}. {role}: {content}")

    print(f"\nTotal items: {len(current_items)}")

    # Demonstrate conversation branching
    print("\n=== Conversation Branching Demo ===")
    print("Let's say we want to edit the conversation from turn 2 onwards...")

    # Soft delete from turn 2 to create a branch point
    print("\nSoft deleting from turn 2 onwards to create branch point...")
    deleted = await session.soft_delete_from_turn(2)
    print(f"Deleted: {deleted}")

    # Show only active items (turn 1 only)
    active_items = await session.get_items()
    print(f"Active items after deletion: {len(active_items)}")
    print("Active conversation (turn 1 only):")
    for i, item in enumerate(active_items, 1):
        role = str(item.get("role", item.get("type", "unknown")))
        if item.get("type") == "function_call":
            content = f"{item.get('name', 'unknown')}({item.get('arguments', '{}')})"
        elif item.get("type") == "function_call_output":
            content = str(item.get("output", ""))
        else:
            content = str(item.get("content", item.get("output", "")))
        print(f"  {i}. {role}: {content}")

    # Create a new branch
    print("\nCreating new conversation branch...")
    print("Turn 2 (new branch): User asks about New York instead")
    result = await Runner.run(
        agent,
        "Actually, what's the weather in New York instead?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    await session.store_run_usage(result)

    # Continue the new branch
    print("Turn 3 (new branch): User asks about NYC attractions")
    result = await Runner.run(
        agent,
        "What are some famous attractions in New York?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    await session.store_run_usage(result)

    # Show the new conversation
    print("\n=== New Conversation Branch ===")
    new_conversation = await session.get_items()
    print("New conversation with branch:")
    for i, item in enumerate(new_conversation, 1):
        role = str(item.get("role", item.get("type", "unknown")))
        if item.get("type") == "function_call":
            content = f"{item.get('name', 'unknown')}({item.get('arguments', '{}')})"
        elif item.get("type") == "function_call_output":
            content = str(item.get("output", ""))
        else:
            content = str(item.get("content", item.get("output", "")))
        print(f"  {i}. {role}: {content}")

    # Show that we can still access the original branch
    all_items = await session.get_items(include_inactive=True)
    print(f"\nTotal items including inactive (original + new branch): {len(all_items)}")

    print("\n=== Conversation Structure Analysis ===")
    # Show conversation turns (active only)
    conversation_turns = await session.get_conversation_by_turns()
    print("Active conversation turns:")
    for turn_num, items in conversation_turns.items():
        print(f"  Turn {turn_num}: {len(items)} items")
        for item in items:  # type: ignore
            if item["tool_name"]:  # type: ignore
                print(
                    f"    - {item['type']} (tool: {item['tool_name']}) [active: {item['active']}]"  # type: ignore
                )
            else:
                print(f"    - {item['type']} [active: {item['active']}]")  # type: ignore

    # Show all conversation turns (including inactive)
    all_conversation_turns = await session.get_conversation_by_turns(include_inactive=True)
    print("\nAll conversation turns (including inactive):")
    for turn_num, items in all_conversation_turns.items():
        print(f"  Turn {turn_num}: {len(items)} items")
        for item in items:  # type: ignore
            status = "ACTIVE" if item["active"] else "INACTIVE"  # type: ignore
            if item["tool_name"]:  # type: ignore
                print(f"    - {item['type']} (tool: {item['tool_name']}) [{status}]")  # type: ignore
            else:
                print(f"    - {item['type']} [{status}]")

    print("\n=== Reactivation Demo ===")
    print("We can also reactivate the original conversation...")

    # Reactivate the original conversation
    reactivated = await session.reactivate_from_turn(2)
    print(f"Reactivated: {reactivated}")

    # Show all active items now
    all_active = await session.get_items()
    print(f"All active items after reactivation: {len(all_active)}")

    print("\n=== Advanced Example Complete ===")
    print("This demonstrates how soft deletion enables conversation editing/branching!")
    print("You can 'undo' parts of a conversation and continue from any point.")
    print("Perfect for building conversational AI systems with editing capabilities.")


if __name__ == "__main__":
    asyncio.run(main())
