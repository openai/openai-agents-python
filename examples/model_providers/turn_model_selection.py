"""Switch models after an SDK tool reports a failed test.

Run with --base-model NAME --review-model NAME. Add --codex to use an installed
Codex wheel. Both names must be available through the same provider.
"""

import argparse
import asyncio
from dataclasses import dataclass

from agents import Agent, ModelSelectionData, RunConfig, RunContextWrapper, Runner
from agents.decorators import tool
from agents.models.codex import CodexProvider


@dataclass
class TestJob:
    failed: bool = False


@tool
def run_tests(ctx: RunContextWrapper[TestJob]) -> str:
    """Return a simulated test result for this example."""
    ctx.context.failed = True
    return "FAILED test_total: expected 42, got 41"


async def main(base_model: str, review_model: str, use_codex: bool) -> None:
    def select_model(turn: ModelSelectionData[TestJob]) -> str | None:
        if turn.context is not None and turn.context.failed:
            return review_model
        return None

    agent = Agent[TestJob](
        name="Test reviewer",
        model=base_model,
        instructions="Call run_tests once. Then explain the reported failure without editing files.",
        tools=[run_tests],
    )
    config = (
        RunConfig(model_provider=CodexProvider(), model_selector=select_model)
        if use_codex
        else RunConfig(model_selector=select_model)
    )
    try:
        result = await Runner.run(agent, "Check the tests.", context=TestJob(), run_config=config)
        print(result.final_output)
    finally:
        await config.model_provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--review-model", required=True)
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.base_model, args.review_model, args.codex))
