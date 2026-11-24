"""Change a flight search in progress, then ask a separate follow-up.

Run with Python's default provider or pass --codex to use CodexProvider.
The flight data is fictional. Both providers use a live model.
"""

import argparse
import asyncio

from agents import Agent, ItemHelpers, MessageReceipt, RunConfig, Runner
from agents.decorators import tool
from agents.models.codex import CodexProvider


async def main(use_codex: bool) -> None:
    search_started = asyncio.Event()
    finish_search = asyncio.Event()

    @tool
    async def search_flights() -> str:
        """Search for flights from Seattle to Boston. Prices are per person."""
        search_started.set()
        await finish_search.wait()
        return "Flight A: $220, one stop. Flight B: $310, nonstop. Flight C: $350, nonstop."

    agent = Agent(
        name="Flight assistant",
        instructions="Call search_flights once. Use its results for the search and follow-ups.",
        tools=[search_flights],
    )
    config = RunConfig(model_provider=CodexProvider()) if use_codex else RunConfig()
    try:
        stream = Runner.run_streamed(
            agent, "Find flights from Seattle to Boston.", run_config=config
        )

        async def simulate_ui() -> tuple[MessageReceipt, MessageReceipt]:
            # A real UI sends each message from its own event handler.
            await search_started.wait()
            try:
                steering = stream.steer("Only show nonstop flights.")
                follow_up = stream.queue("What would two seats on the cheapest nonstop cost?")
                return steering, follow_up
            finally:
                finish_search.set()

        ui = asyncio.create_task(simulate_ui())
        try:
            # The stream shows both answers. At the end, stream.final_output holds the last one.
            async for event in stream.stream_events():
                if (
                    event.type == "run_item_stream_event"
                    and event.item.type == "message_output_item"
                ):
                    print(ItemHelpers.text_message_output(event.item))
            if search_started.is_set():
                steering, follow_up = await ui
                print("Steering:", (await steering.wait()).status)
                print("Follow-up:", (await follow_up.wait()).status)
        finally:
            # The run can finish without calling the search tool.
            ui.cancel()
            await asyncio.gather(ui, return_exceptions=True)
    finally:
        await config.model_provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true")
    asyncio.run(main(parser.parse_args().codex))
