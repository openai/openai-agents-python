from __future__ import annotations

import asyncio
from agents import Agent, Runner, function_tool
from agents.exceptions import UserError


"""
This example demonstrates raising a `UserError` at runtime by violating a tool-specific logic rule
(e.g., returning the wrong type despite declaring a valid return type).

This passes `mypy` but fails at runtime due to logical misuse of the SDK.
"""


@function_tool
def invalid_tool() -> None:
    # This tool *claims* to return None, but it returns a string instead.
    # This misuse is caught by the SDK during runtime and raises UserError.
    return "This return violates the declared return type."


async def main():
    agent = Agent(
        name="Assistant",
        instructions="Use the tool to demonstrate misuse.",
        tools=[invalid_tool],
        tool_use_behavior="run_llm_again",  # ✅ valid value — no mypy error
    )

    user_input = "Please do something invalid"
    try:
        result = await Runner.run(agent, user_input)
        print(result.final_output)
    except UserError as e:
        print(f"UserError caught as expected: {e}")


if __name__ == "__main__":
    asyncio.run(main())
