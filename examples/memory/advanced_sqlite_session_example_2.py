"""
Advanced example demonstrating conversation branching with AdvancedSQLiteSession.

This example shows how to use the new conversation branching system,
allowing you to branch conversations from any user message and manage multiple timelines.
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

    # Create an advanced session instance
    session = AdvancedSQLiteSession(
        session_id="conversation_advanced",
        create_tables=True,
    )

    print("=== Advanced Session: Conversation Branching ===")
    print("This example demonstrates the new conversation branching system.\n")

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
    print("Let's explore a different path from turn 2...")

    # Show available turns for branching
    print("\nAvailable turns for branching:")
    turns = await session.get_conversation_turns()
    for turn in turns:
        print(f"  Turn {turn['turn']}: {turn['content']}")

    # Create a branch from turn 2
    print("\nCreating new branch from turn 2...")
    branch_id = await session.create_branch_from_turn(2)
    print(f"Created branch: {branch_id}")

    # Show what's in the new branch (should have conversation up to turn 2)
    branch_items = await session.get_items()
    print(f"Items copied to new branch: {len(branch_items)}")
    print("New branch contains:")
    for i, item in enumerate(branch_items, 1):
        role = str(item.get("role", item.get("type", "unknown")))
        if item.get("type") == "function_call":
            content = f"{item.get('name', 'unknown')}({item.get('arguments', '{}')})"
        elif item.get("type") == "function_call_output":
            content = str(item.get("output", ""))
        else:
            content = str(item.get("content", item.get("output", "")))
        print(f"  {i}. {role}: {content}")

    # Continue conversation in new branch
    print("\nContinuing conversation in new branch...")
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

    print(f"\nTotal items in new branch: {len(new_conversation)}")

    print("\n=== Branch Management ===")
    # Show all branches
    branches = await session.list_branches()
    print("All branches in this session:")
    for branch in branches:
        current = " (current)" if branch["is_current"] else ""
        print(
            f"  {branch['branch_id']}: {branch['user_turns']} user turns, {branch['message_count']} total messages{current}"
        )

    # Show conversation turns in current branch
    print("\nConversation turns in current branch:")
    current_turns = await session.get_conversation_turns()
    for turn in current_turns:
        print(f"  Turn {turn['turn']}: {turn['content']}")

    print("\n=== Branch Switching Demo ===")
    print("We can switch back to the main branch...")

    # Switch back to main branch
    await session.switch_to_branch("main")
    print("Switched to main branch")

    # Show what's in main branch
    main_items = await session.get_items()
    print(f"Items in main branch: {len(main_items)}")

    # Switch back to new branch
    await session.switch_to_branch(branch_id)
    branch_items = await session.get_items()
    print(f"Items in new branch: {len(branch_items)}")

    print("\n=== Final Summary ===")
    await session.switch_to_branch("main")
    main_final = len(await session.get_items())
    await session.switch_to_branch(branch_id)
    branch_final = len(await session.get_items())

    print(f"Main branch items: {main_final}")
    print(f"New branch items: {branch_final}")

    # Show that branches are completely independent
    print("\nBranches are completely independent:")
    print("- Main branch has full original conversation")
    print("- New branch has turn 1 + new conversation path")
    print("- No interference between branches!")

    print("\n=== Advanced Example Complete ===")
    print("This demonstrates the new conversation branching system!")
    print("Key features:")
    print("- Create branches from any user message")
    print("- Branches inherit conversation history up to the branch point")
    print("- Complete branch isolation - no interference between branches")
    print("- Easy branch switching and management")
    print("- No complex soft deletion - clean branch-based architecture")
    print("- Perfect for building AI systems with conversation editing capabilities!")

    # Cleanup
    session.close()


if __name__ == "__main__":
    asyncio.run(main())
