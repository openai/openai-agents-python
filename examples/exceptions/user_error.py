from __future__ import annotations

import asyncio

from agents import Agent, Runner, function_tool
from agents.exceptions import UserError

"""
This example demonstrates raising a `UserError` manually during tool execution.
This avoids mypy errors but simulates incorrect SDK usage or logic issues.
"""


@function_tool
def invalid_tool() -> str:
    # Simulate misuse or invalid condition
    raise UserError("This tool was misused and raised a UserError intentionally.")


async def main():
    agent = Agent(
        name="Assistant",
        instructions="Use the tool to demonstrate a manual UserError.",
        tools=[invalid_tool],
        tool_use_behavior="run_llm_again",  # ✅ valid, passes mypy
    )

    user_input = "Trigger the error"
    try:
        result = await Runner.run(agent, user_input)
        print(result.final_output)
    except UserError as e:
        print(f"UserError caught as expected: {e}")


if __name__ == "__main__":
    asyncio.run(main())
