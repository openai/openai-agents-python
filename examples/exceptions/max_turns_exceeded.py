from __future__ import annotations
import asyncio
from agents import Agent, Runner, function_tool
from agents.exceptions import MaxTurnsExceeded

"""
This example demonstrates an OpenAI Agents SDK agent that triggers a MaxTurnsExceeded error.

The 'TriageAgent' handles user queries using tools for fetching weather (`get_weather`) or adding numbers (`sum_numbers`). The instructions direct the agent to process both tasks in a single turn, but with `max_turns=1`, this causes a `MaxTurnsExceeded` error. The interactive loop processes user queries as direct string inputs, catching and displaying the `MaxTurnsExceeded` error message.
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
        instructions="Process both get_weather and sum_numbers in a single turn when asked for both.",
        tools=[sum_numbers, get_weather],
    )

    user_input = "What is US Weather and sum 2 + 2."
    try:
        result = await Runner.run(agent, user_input, max_turns=1)
        print(result.final_output)
    except MaxTurnsExceeded as e:
        print(f"Caught MaxTurnsExceeded: {e}")
    


if __name__ == "__main__":
    asyncio.run(main())
