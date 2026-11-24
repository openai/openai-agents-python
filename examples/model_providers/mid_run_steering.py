"""Send a correction while an SDK tool runs, with either Python or CodexProvider.

The tool signals when it starts so this example can simulate a user sending a
message from the UI. A real UI can call control.send from its event handler.
"""

import argparse
import asyncio

from agents import Agent, RunConfig, RunControl, Runner
from agents.decorators import tool
from agents.models.codex import CodexProvider


async def main(use_codex: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    @tool
    async def run_tests() -> str:
        """Return a simulated test result after the user has had time to respond."""
        started.set()
        await release.wait()
        return "FAILED test_total: expected 42, got 41"

    agent = Agent(
        name="Test reviewer",
        instructions="Call run_tests once, then respond to the user. Do not edit files.",
        tools=[run_tests],
    )
    config = RunConfig(model_provider=CodexProvider()) if use_codex else RunConfig()
    control = RunControl()
    run = asyncio.create_task(
        Runner.run(agent, "Run the tests.", run_config=config, control=control)
    )
    tool_started = asyncio.create_task(started.wait())
    try:
        done, _ = await asyncio.wait((run, tool_started), return_when=asyncio.FIRST_COMPLETED)
        if run in done:
            print((await run).final_output)
            return
        receipt = control.send("Explain the failure before proposing a fix.")
        release.set()
        result = await run
        outcome = await receipt.wait()
        print(f"Correction: {outcome.status} ({outcome.reason})")
        print(result.final_output)
    finally:
        release.set()
        for task in (tool_started, run):
            task.cancel()
        await asyncio.gather(tool_started, run, return_exceptions=True)
        await config.model_provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true")
    asyncio.run(main(parser.parse_args().codex))
