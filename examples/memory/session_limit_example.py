"""
Example demonstrating SessionSettings(limit=N) via RunConfig.

Use session_settings to cap how much history is retrieved from a session before
each run. This is useful for long conversations where you want to bound context size.
"""

import asyncio

from agents import Agent, RunConfig, Runner, SessionSettings, SQLiteSession


async def main():
    agent = Agent(
        name="Assistant",
        instructions="Reply very concisely.",
    )

    session = SQLiteSession("session_limit_demo")

    print("=== Session Limit Example ===")
    print("Only the most recent 2 history items are sent to the model each turn.\n")

    prompts = [
        "What city is the Golden Gate Bridge in?",
        "What state is it in?",
        "What's the population of that state?",
        "What country is that state part of?",
    ]

    for index, prompt in enumerate(prompts, start=1):
        print(f"Turn {index}:")
        print(f"User: {prompt}")
        result = await Runner.run(
            agent,
            prompt,
            session=session,
            run_config=RunConfig(session_settings=SessionSettings(limit=2)),
        )
        print(f"Assistant: {result.final_output}\n")

    all_items = await session.get_items()
    print(f"Total items stored in session: {len(all_items)}")
    print("All turns are persisted, but each run only retrieves the latest 2 items.")


if __name__ == "__main__":
    asyncio.run(main())
