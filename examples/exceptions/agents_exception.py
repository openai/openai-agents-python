from __future__ import annotations

import asyncio
from agents import Agent, Runner, function_tool
from agents.exceptions import AgentsException


"""
This example demonstrates the use of the OpenAI Agents SDK with tools and comprehensive error handling.

The agent, 'Triage Agent', is configured to handle two tasks:
- Fetching weather information for a specified city using the `get_weather` tool.
- Adding two numbers using the `sum_numbers` tool.

The agent is instructed to use only one tool per execution cycle and can switch to another tool in subsequent cycles.
The example sets a `max_turns=1` limit to intentionally restrict the agent to a single turn, which may trigger a `MaxTurnsExceeded` error.

All exceptions are caught via `AgentsException`, the base class for SDK errors.
"""

# Define tools

@function_tool
async def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny."


@function_tool
async def sum_numbers(a: int, b: int) -> str:
    """Adds two numbers."""
    result = a + b
    return f"The sum of {a} and {b} is {result}."


agent = Agent(
    name="Triage Agent",
    instructions="Get weather or sum numbers. Use only one tool per turn.",
    tools=[get_weather, sum_numbers],
)


async def main():
    try:
        user_input = input("Enter a message: ")
        result = await Runner.run(agent, user_input, max_turns=1)
        print("✅ Final Output:", result.final_output)
    except AgentsException as e:
        print(f"❌ Caught {e.__class__.__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
