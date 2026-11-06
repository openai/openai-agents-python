"""Continue and restore a native conversation after its work settles."""

import asyncio
from contextlib import suppress

from agents import Agent, Turn
from agents.observation import OutputTextDelta


async def print_text(turn: Turn[str]) -> None:
    async for event in turn.events():
        if isinstance(event, OutputTextDelta):
            print(event.delta, end="", flush=True)


async def main() -> None:
    agent = Agent(instructions="Answer briefly and remember this conversation.")
    async with agent.session() as session:
        first = await session.submit("Remember the label atlas.")
        await first.get_result()
        second = await session.submit("What label did I give you?")
        observer = asyncio.create_task(print_text(second))
        try:
            result = await second.get_result()
        finally:
            observer.cancel()
            with suppress(asyncio.CancelledError):
                await observer
        print("\nFinal answer:", result.final_output)
        state = await session.save_state()

    async with agent.session(state=state) as restored:
        third = await restored.submit("Repeat that label once more.")
        result = await third.get_result()
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
