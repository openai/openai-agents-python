import asyncio

from examples.auto_mode import input_with_fallback

from .manager import FinancialResearchManager


# Entrypoint for the financial bot example.
# Run this as `python -m examples.financial_research_agent.main` and enter a
# financial research query, for example:
# "Write up an analysis of Apple Inc.'s most recent quarter."
async def main() -> None:
    query = input_with_fallback(
        "Enter a financial research query: ",
        "Write up an analysis of Apple Inc.'s most recent quarter.",
    )
    mgr = FinancialResearchManager()
    await mgr.run(query)


if __name__ == "__main__":
    asyncio.run(main())
