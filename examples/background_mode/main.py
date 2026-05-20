"""Example demonstrating Responses API background mode.

When `ModelSettings(background=True)` is set, the SDK submits the underlying
`client.responses.create()` call with `background=True` and adaptively polls
`client.responses.retrieve(...)` until the response reaches a terminal state.
This lets long-running reasoning calls (gpt-5.2-pro, deep-research-class
workloads) survive HTTP / proxy / serverless timeouts that would otherwise
abort a synchronous call.

To run this example:

    export OPENAI_API_KEY=...
    python -m examples.background_mode.main

Compare the two runs below: with and without `background=True`. The output
should be equivalent, but only the background variant keeps the server-side
work alive across transient client-side disconnects.
"""

from __future__ import annotations

import asyncio
import os

from agents import Agent, ModelSettings, Runner

MODEL_NAME = os.getenv("BACKGROUND_MODEL_NAME") or "gpt-5.2-pro"
PROMPT = (
    "Plan a three-stage research workflow for studying the long-term effects "
    "of intermittent fasting on cognitive performance. For each stage, list "
    "the primary research question, the methods, and one specific risk to "
    "external validity."
)


async def run_synchronous() -> str:
    agent = Agent(name="planner", model=MODEL_NAME)
    print("\n=== Without background mode (synchronous) ===")
    result = await Runner.run(agent, PROMPT)
    return str(result.final_output)


async def run_background() -> str:
    agent = Agent(
        name="planner",
        model=MODEL_NAME,
        model_settings=ModelSettings(background=True),
    )
    print("\n=== With background mode (submit + adaptive poll) ===")
    result = await Runner.run(agent, PROMPT)
    return str(result.final_output)


async def main() -> None:
    try:
        sync_output = await run_synchronous()
        print(sync_output)

        bg_output = await run_background()
        print(bg_output)

        # The two transports should produce equivalent final output for the
        # same prompt and seed. Background mode's win is durability, not
        # different content.
        if sync_output.strip() == bg_output.strip():
            print("\nOutputs match.")
        else:
            print(
                "\nOutputs differ — expected when sampling is non-deterministic, "
                "but the background variant survived any transient disconnects."
            )
    except Exception as exc:
        print(f"Error: {exc}")
        print("\nNote: background mode is supported only by the Responses API")
        print("HTTP transport. Set OPENAI_API_KEY and try a model that")
        print("accepts long-running background requests (e.g. gpt-5.2-pro).")


if __name__ == "__main__":
    asyncio.run(main())
