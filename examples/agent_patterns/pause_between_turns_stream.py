from __future__ import annotations

import asyncio

from openai.types.responses import ResponseTextDeltaEvent

from agents import Agent, RawResponsesStreamEvent, Runner, TResponseInputItem, function_tool

"""
This example shows how to stop a streamed run after the current turn, inject a new user message,
and continue using only public result surfaces.
"""


@function_tool
async def fetch_inventory(sku: str) -> str:
    """Look up inventory for a stock-keeping unit."""
    await asyncio.sleep(1)
    return f"{sku}: 3 new units, 1 refurbished unit"


agent = Agent(
    name="Inventory Assistant",
    instructions=(
        "When the user asks about stock, call `fetch_inventory` first. "
        "After the tool returns, summarize the available inventory."
    ),
    tools=[fetch_inventory],
)


async def main():
    result = Runner.run_streamed(agent, "Check warehouse stock for sku-123.")
    pause_requested = False

    async for event in result.stream_events():
        if isinstance(event, RawResponsesStreamEvent) and isinstance(
            event.data, ResponseTextDeltaEvent
        ):
            print(event.data.delta, end="", flush=True)
        elif (
            event.type == "run_item_stream_event"
            and event.name == "tool_called"
            and not pause_requested
        ):
            print("\n\nHost received a higher-priority user update. Pausing after this turn...\n")
            result.cancel(mode="after_turn")
            pause_requested = True

    if not pause_requested:
        print("\nRun finished before an external update arrived.")
        return

    next_input: list[TResponseInputItem] = result.to_input_list(mode="normalized")
    next_input.append(
        {
            "role": "user",
            "content": "Also include refurbished inventory if available.",
        }
    )

    print("Resuming with the new user input...\n")
    result = Runner.run_streamed(result.last_agent, next_input)
    async for event in result.stream_events():
        if isinstance(event, RawResponsesStreamEvent) and isinstance(
            event.data, ResponseTextDeltaEvent
        ):
            print(event.data.delta, end="", flush=True)

    print("\n")


if __name__ == "__main__":
    asyncio.run(main())
