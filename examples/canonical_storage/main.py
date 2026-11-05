"""Run twice with the same database and printed Session ID to reopen history."""

import argparse
import asyncio
from pathlib import Path

from agents import Agent, CanonicalStorage, NativeProvider, SQLiteThreadStore, function_tool


@function_tool
def order_status(order_id: str) -> str:
    """Look up the current status of a synthetic order."""
    return f"Order {order_id} has shipped."


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--resume")
    parser.add_argument("--base-url", help="Optional current provider endpoint")
    parser.add_argument("--prompt", default="Check order example-123 using the tool.")
    options = parser.parse_args()
    storage = CanonicalStorage(SQLiteThreadStore(options.database))
    async with NativeProvider(base_url=options.base_url) as provider:
        agent = Agent(
            instructions="Use the order tool when asked.",
            model="gpt-5.4",
            tools=[order_status],
            provider=provider,
        )
        async with agent.session(storage=storage, resume=options.resume) as session:
            print("Session ID:", session.id)
            turn = await session.submit(options.prompt)
            print((await turn.get_result()).final_output)


if __name__ == "__main__":
    asyncio.run(main())
