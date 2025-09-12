"""
Basic example demonstrating advanced session memory functionality.

This example shows how to use AdvancedSQLiteSession for conversation tracking
with usage statistics and turn-based organization.
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
    session = AdvancedSQLiteSession(session_id="conversation_basic")

    print("=== Basic Advanced Session Example ===")
    print("The agent will remember previous messages with structured tracking.\n")

    # First turn
    print("First turn:")
    print("User: What city is the Golden Gate Bridge in?")
    result = await Runner.run(
        agent,
        "What city is the Golden Gate Bridge in?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    print(f"Usage: {result.context_wrapper.usage.total_tokens} tokens")

    # Store usage data automatically
    await session.store_run_usage(result)
    print()

    # Second turn - continuing the conversation
    print("Second turn:")
    print("User: What's the weather in that city?")
    result = await Runner.run(
        agent,
        "What's the weather in that city?",
        session=session,
    )
    print(f"Assistant: {result.final_output}")
    print(f"Usage: {result.context_wrapper.usage.total_tokens} tokens")

    # Store usage data automatically
    await session.store_run_usage(result)
    print()

    print("=== Usage Tracking Demo ===")
    session_usage = await session.get_session_usage()
    if session_usage:
        print("Session Usage (aggregated from turns):")
        print(f"  Total requests: {session_usage['requests']}")
        print(f"  Total tokens: {session_usage['total_tokens']}")
        print(f"  Input tokens: {session_usage['input_tokens']}")
        print(f"  Output tokens: {session_usage['output_tokens']}")
        print(f"  Total turns: {session_usage['total_turns']}")

        # Show usage by turn
        turn_usage_list = await session.get_turn_usage()
        if turn_usage_list and isinstance(turn_usage_list, list):
            print("\nUsage by turn:")
            for turn_data in turn_usage_list:
                turn_num = turn_data["user_turn_number"]
                tokens = turn_data["total_tokens"]
                print(f"  Turn {turn_num}: {tokens} tokens")
    else:
        print("No usage data found.")

    print("\n=== Structured Query Demo ===")
    conversation_turns = await session.get_conversation_by_turns()
    print("Conversation by turns:")
    for turn_num, items in conversation_turns.items():
        print(f"  Turn {turn_num}: {len(items)} items")
        for item in items:
            if item["tool_name"]:
                print(f"    - {item['type']} (tool: {item['tool_name']})")
            else:
                print(f"    - {item['type']}")

    # Show tool usage
    tool_usage = await session.get_tool_usage()
    if tool_usage:
        print("\nTool usage:")
        for tool_name, count, turn in tool_usage:
            print(f"  {tool_name}: used {count} times in turn {turn}")
    else:
        print("\nNo tool usage found.")

    print("\n=== Basic Example Complete ===")
    print("Session provides structured tracking with usage analytics!")


if __name__ == "__main__":
    asyncio.run(main())
