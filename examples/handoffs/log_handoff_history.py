from __future__ import annotations

import asyncio
import json

from agents import Agent, HandoffInputData, Runner, handoff
from agents.extensions.handoff_filters import nest_handoff_history
from agents.items import ItemHelpers


math_agent = Agent(
    name="Math Agent",
    instructions=(
        "You are a friendly math expert. Explain your reasoning and finish with a clear answer."
    ),
)


def log_handoff_history(data: HandoffInputData) -> HandoffInputData:
    """Print the transcript that will be forwarded to the next agent."""

    nested = nest_handoff_history(data)
    history_items = (
        nested.input_history
        if isinstance(nested.input_history, tuple)
        else tuple(ItemHelpers.input_to_new_input_list(nested.input_history))
    )

    print("\n--- Handoff transcript ---")
    for idx, item in enumerate(history_items, start=1):
        print(f"Turn {idx}: {json.dumps(item, indent=2, ensure_ascii=False)}")
    print("--- end of transcript ---\n")

    return nested


router_agent = Agent(
    name="Router",
    instructions=(
        "You greet the user and then call the math handoff tool whenever the user asks for"
        " calculation help so the specialist can respond."
    ),
    handoffs=[handoff(math_agent, input_filter=log_handoff_history)],
)


async def main() -> None:
    result = await Runner.run(
        router_agent,
        "Hi there! Could you compute 784 + 219 and explain how you got the result?",
    )
    print("Final output:\n", result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
