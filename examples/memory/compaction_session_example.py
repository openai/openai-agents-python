"""
Example demonstrating OpenAI responses.compact session functionality.

This example shows how to use OpenAIResponsesCompactionSession to automatically
compact conversation history when it grows too large, reducing token usage
while preserving context.
"""

import asyncio

from agents import Agent, OpenAIResponsesCompactionSession, Runner, SQLiteSession


async def main():
    # Create an underlying session for storage
    underlying = SQLiteSession(":memory:")

    # Wrap with compaction session - will automatically compact when threshold hit
    session = OpenAIResponsesCompactionSession(
        session_id="demo-session",
        underlying_session=underlying,
        model="gpt-4.1",
        # Custom compaction trigger (default is 10 candidates)
        should_trigger_compaction=lambda ctx: len(ctx["compaction_candidate_items"]) >= 4,
    )

    agent = Agent(
        name="Assistant",
        instructions="Reply concisely. Keep answers to 1-2 sentences.",
    )

    print("=== Compaction Session Example ===\n")

    prompts = [
        "What is the tallest mountain in the world?",
        "How tall is it in feet?",
        "When was it first climbed?",
        "Who was on that expedition?",
        "What country is the mountain in?",
    ]

    for i, prompt in enumerate(prompts, 1):
        print(f"Turn {i}:")
        print(f"User: {prompt}")
        result = await Runner.run(agent, prompt, session=session)
        print(f"Assistant: {result.final_output}\n")

    # Show final session state
    items = await session.get_items()
    print("=== Final Session State ===")
    print(f"Total items: {len(items)}")
    for item in items:
        # Some inputs are stored as easy messages (only `role` and `content`).
        item_type = item.get("type") or ("message" if "role" in item else "unknown")
        if item_type == "compaction":
            print("  - compaction (encrypted content)")
        elif item_type == "message":
            role = item.get("role", "unknown")
            print(f"  - message ({role})")
        else:
            print(f"  - {item_type}")


if __name__ == "__main__":
    asyncio.run(main())
