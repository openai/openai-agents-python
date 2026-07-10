"""Example 00: Environment setup check.

Prints SDK version, Python, dependencies, and model backend connectivity.
Usage: python examples/00_setup_check.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def check_static():
    import agents
    from agents.run import Runner

    print("=== Static Environment ===")
    print(f"  Python:     {sys.version.split()[0]}")
    print(f"  openai-agents: {agents.__version__ if hasattr(agents, '__version__') else 'unknown'}")
    try:
        from importlib.metadata import version

        print(f"  openai:     {version('openai-agents')} (meta)")
        print(f"  pydantic:   {version('pydantic')}")
        print(f"  griffe:     {version('griffe')}")
    except Exception as e:
        print(f"  meta lookup: {e}")
    print(f"  Runner entrypoints: {[m for m in dir(Runner) if m.startswith('run_')]}")


async def check_live():
    from config import BASE_URL, MODEL, MODEL_NAME  # type: ignore[import-not-found]

    from agents import Agent, Runner

    print("\n Runtime ")
    print(f"  base_url:   {BASE_URL}")
    print(f"  model:      {MODEL_NAME}")

    agent = Agent(
        name="SetupCheck",
        instructions="Reply in one sentence.",
        model=MODEL,
    )
    result = await Runner.run(agent, "ping")
    print(f"  final:      {result.final_output!r}")
    print(f"  raw count:  {len(result.raw_responses)}")
    print(f"  new_items:  {len(result.new_items)}")
    print(f"  usage:      {result.context_wrapper.usage}")


if __name__ == "__main__":
    check_static()
    asyncio.run(check_live())
    print("\n Setup OK. Proceed to run 01 -> 15 in order.")
