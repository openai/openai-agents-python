from __future__ import annotations
import asyncio
from agents import Agent, Runner, function_tool
from agents.exceptions import MaxTurnsExceeded
"""
This example demonstrates an OpenAI Agents SDK agent that triggers a MaxTurnsExceeded error.

The 'TriageAgent' handles user queries using tools for fetching weather (`get_weather`) or adding numbers (`sum_numbers`). The agent is instructed to use one tool per execution cycle. With `max_turns=1`, attempting to process multiple tasks (e.g., weather and sum) in one input causes a `MaxTurnsExceeded` error. The interactive loop processes user queries as direct string inputs, catching and displaying the `MaxTurnsExceeded` error message.
"""
@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

@function_tool
def sum_numbers(a: int, b: int) -> int:
    """Adds two numbers."""
    return a + b

async def main():
    agent = Agent(
        name="TriageAgent",
        instructions="Get weather or sum numbers. Use one tool at a time, switching to another tool in subsequent turns.",
        tools=[sum_numbers, get_weather],
    )

    while True:
        user_input = input("Enter a message: ")
        try:
            result = await Runner.run(agent, user_input, max_turns=1)
            print(result.final_output)
        except MaxTurnsExceeded as e:
            print(f"Error: {e}")
            break

if __name__ == "__main__":
    asyncio.run(main())