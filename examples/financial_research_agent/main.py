import argparse
import asyncio

from agents import RunConfig
from agents.models.codex import CodexProvider
from examples.auto_mode import input_with_fallback

from .manager import FinancialResearchManager


# Entrypoint for the financial bot example.
# Run this as `python -m examples.financial_research_agent.main` and enter a
# financial research query, for example:
# "Write up an analysis of Apple Inc.'s most recent quarter."
async def main(use_codex: bool = False) -> None:
    query = input_with_fallback(
        "Enter a financial research query: ",
        "Write a short analysis of Apple's long-term revenue drivers and key risks. "
        "Avoid making claims about unreleased quarterly results.",
    )
    config = RunConfig(model_provider=CodexProvider()) if use_codex else RunConfig()
    mgr = FinancialResearchManager(run_config=config)
    await mgr.run(query)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run sourced financial research.")
    parser.add_argument("--codex", action="store_true", help="Use the stock Codex model provider")
    asyncio.run(main(parser.parse_args().codex))
