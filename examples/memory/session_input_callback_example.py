"""
Example demonstrating RunConfig.session_input_callback for custom history merging.

Use session_input_callback when you need to prune, reorder, or filter session history
before each model call without changing how the session stores new turn items.
"""

import asyncio

from agents import Agent, RunConfig, Runner, SQLiteSession


def keep_recent_history(history, new_input):
    """Keep only the last 4 history items, then append the new turn."""
    return history[-4:] + new_input


async def main():
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    session = SQLiteSession("session_input_callback_demo")

    print("=== Session Input Callback Example ===")
    print("History is pruned to the last 4 items before each model call.\n")

    prompts = [
        "Name one planet in our solar system.",
        "Name another planet.",
        "Name a third planet.",
        "Name a fourth planet.",
        "Which planet did you mention first?",
    ]

    run_config = RunConfig(session_input_callback=keep_recent_history)

    for index, prompt in enumerate(prompts, start=1):
        print(f"Turn {index}:")
        print(f"User: {prompt}")
        result = await Runner.run(
            agent,
            prompt,
            session=session,
            run_config=run_config,
        )
        print(f"Assistant: {result.final_output}\n")

    all_items = await session.get_items()
    print(f"Total items stored in session: {len(all_items)}")
    print("The full conversation is still persisted even though older history")
    print("may be excluded from individual model calls.")


if __name__ == "__main__":
    asyncio.run(main())
