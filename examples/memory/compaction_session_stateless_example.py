"""
Example demonstrating stateless compaction with store=False.

In auto mode, OpenAIResponsesCompactionSession uses input-based compaction when
responses are not stored on the server.
"""

import asyncio

from agents import Agent, ModelSettings, OpenAIResponsesCompactionSession, Runner, SQLiteSession


async def main():
    # Create an underlying session for storage
    underlying = SQLiteSession(":memory:")

    # Wrap with compaction session in auto mode. When store=False, this will
    # compact using the locally stored input items.
    session = OpenAIResponsesCompactionSession(
        session_id="demo-session",
        underlying_session=underlying,
        model="gpt-4.1",
        compaction_mode="auto",
        should_trigger_compaction=lambda ctx: len(ctx["compaction_candidate_items"]) >= 3,
    )

    agent = Agent(
        name="Assistant",
        instructions="Reply concisely. Keep answers to 1-2 sentences.",
        model_settings=ModelSettings(store=False),
    )

    print("=== Stateless Compaction Session Example ===\n")

    prompts = [
        "What is the tallest mountain in the world?",
        "How tall is it in feet?",
        "When was it first climbed?",
        "Who was on that expedition?",
    ]

    for i, prompt in enumerate(prompts, 1):
        print(f"Turn {i}:")
        print(f"User: {prompt}")
        result = await Runner.run(agent, prompt, session=session)
        print(f"Assistant: {result.final_output}\n")

    # Show session state after automatic compaction (if triggered)
    items = await session.get_items()
    print("=== Session State (Auto Compaction) ===")
    print(f"Total items: {len(items)}")
    for item in items:
        item_type = item.get("type") or ("message" if "role" in item else "unknown")
        if item_type == "compaction":
            print("  - compaction (encrypted content)")
        elif item_type == "message":
            role = item.get("role", "unknown")
            print(f"  - message ({role})")
        else:
            print(f"  - {item_type}")
    print()

    # Manual compaction in stateless mode.
    print("=== Manual Compaction ===")
    await session.run_compaction({"force": True})
    print("Done")
    print()

    # Show final session state
    items = await session.get_items()
    print("=== Final Session State ===")
    print(f"Total items: {len(items)}")
    for item in items:
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
