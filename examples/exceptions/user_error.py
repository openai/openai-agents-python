from __future__ import annotations

import asyncio

from agents import Agent, Runner, function_tool
from agents.exceptions import UserError

"""
This example demonstrates an OpenAI Agents SDK agent that triggers a UserError due to incorrect SDK usage.

The 'Assistant' agent is configured with an invalid `tool_use_behavior` (empty string) and an invalid tool (`invalid_tool`) that declares a `None` return type but returns a string. Either issue raises a `UserError` when the agent is executed, indicating improper SDK configuration by the user. The interactive loop processes user queries as direct string inputs, catching and displaying the `UserError` message.
"""


@function_tool
def invalid_tool() -> None:
    return "I return a string"  # Type mismatch triggers UserError


async def main():
    agent = Agent(
        name="Assistant",
        instructions="Use the invalid_tool to process queries.",
        tools=[invalid_tool],
        tool_use_behavior="invalid_tool",
    )
    user_input = "Do Something."
    try:
        result = await Runner.run(agent, user_input)
        print(result.final_output)
    except UserError as e:
        print(f"UserError: {e}")


if __name__ == "__main__":
    asyncio.run(main())
