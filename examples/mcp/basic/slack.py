"""
Example demonstrating how to use an agent with MCP servers for Slack integration.

This example shows how to:
1. Load MCP servers from the config file automatically
2. Create an agent that connects to Slack via MCP tools
3. Run the agent to search through Slack conversations

To use this example:
1. Ensure you have a slack MCP server configured in your mcp_agent.config.yaml
2. Run this example
"""

import asyncio
import os
import sys
import time
from typing import TYPE_CHECKING, Any

from openai.types.responses import ResponseTextDeltaEvent

if TYPE_CHECKING:
    from mcp_agent.config import MCPSettings

# Add the src directory to the path for imports to work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from agents import Agent, Runner

# Enable logging for debugging
# enable_verbose_stdout_logging()


class AgentContext:
    """Context class for the agent that can hold MCP settings."""

    def __init__(self, mcp_config_path: str = None, mcp_config: "MCPSettings" = None):
        """
        Initialize the context.

        Args:
            mcp_config_path: Optional path to the mcp_agent.config.yaml file
            mcp_config: Optional MCPSettings object
        """
        self.mcp_config_path = mcp_config_path
        self.mcp_config = mcp_config


async def main():
    """Run the Slack integration example."""
    start = time.time()

    # Create a context object
    context = AgentContext()

    # Create an agent that specifies which MCP servers to use
    # Make sure these are defined in your mcp_agent.config.yaml file
    agent = Agent(
        name="Slack Finder",
        instructions="""You are an agent with access to the filesystem, 
        as well as the ability to look up Slack conversations. Your job is to identify
        the closest match to a user's request, make the appropriate tool calls,
        and return the results.""",
        # Local tools can be added here if needed
        tools=[],
        # Specify which MCP servers to use
        mcp_servers=["filesystem", "slack"],
    )

    # First example: Search for last message in general channel
    print("\n\n--- FIRST QUERY ---")
    print("Searching for the last message in the general channel...\n")

    result = Runner.run_streamed(
        agent,
        input="What was the last message in the general channel?",
        context=context,
    )

    # Stream the response
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)

    # Second example: Follow-up question demonstrating multi-turn capabilities
    print("\n\n--- FOLLOW-UP QUERY ---")
    print("Asking for a summary of the returned information...\n")

    result = Runner.run_streamed(
        agent,
        input=f"Summarize {result.final_output} for me and save it as convo.txt in the current directory.",
        context=context,
    )

    # Stream the response
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)

    # Calculate and display total runtime
    end = time.time()
    t = end - start
    print(f"\n\nTotal run time: {t:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
