import asyncio

from examples.auto_mode import input_with_fallback

from .manager import ResearchManager


async def main() -> None:
    query = input_with_fallback(
        "What would you like to research? ",
        "Impact of electric vehicles on the grid.",
    )
    await ResearchManager().run(query)


if __name__ == "__main__":
    asyncio.run(main())
