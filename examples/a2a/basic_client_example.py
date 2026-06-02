"""
Example: Calling an A2A agent as a tool from an OpenAI agent.

This example demonstrates using ``A2AClientTool`` to call a remote A2A agent
as a function tool. It assumes you have an A2A agent running at the given URL.

To try it out:

1. Start a sample A2A server (e.g. the hello-world agent from a2a-sdk):
   ``cd a2a-python/samples && python cli.py server``

2. Run this script:
   ``python examples/a2a/basic_client_example.py``
"""

from __future__ import annotations

import asyncio

from agents import Agent, Runner
from agents.extensions.a2a import A2AClientTool


async def main() -> None:
    # Create a tool that wraps a remote A2A agent.
    # from_url() fetches the AgentCard at .well-known/agent-card.json
    research_tool = await A2AClientTool.from_url(
        url="http://localhost:10000",
        tool_name="research_agent",
        tool_description=(
            "Ask the research agent to find and summarize information. "
            "Use when you need external knowledge."
        ),
    )

    orchestrator = Agent(
        name="Orchestrator",
        instructions="You are an orchestrator. Use the research_agent tool for external queries.",
        tools=[research_tool.as_function_tool()],
    )

    result = await Runner.run(
        orchestrator,
        "What are the latest developments in quantum computing?",
    )
    print(f"Final output: {result.final_output}")

    # Clean up
    await research_tool.close()


if __name__ == "__main__":
    asyncio.run(main())
