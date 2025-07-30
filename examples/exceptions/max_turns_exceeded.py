"""
This example demonstrates an OpenAI Agents SDK agent that triggers a MaxTurnsExceeded error.

The 'TriageAgent' handles user queries using tools for fetching weather (`get_weather`) or adding numbers (`sum_numbers`). The agent is instructed to use one tool per execution cycle. With `max_turns=1`, attempting to process multiple tasks (e.g., weather and sum) in one input causes a `MaxTurnsExceeded` error. The interactive loop processes user queries as direct string inputs, catching and displaying the `MaxTurnsExceeded` error message.
"""
from agents import Agent, RunContextWrapper, Runner, function_tool
from agents.exceptions import AgentsException
import asyncio

@function_tool
def get_weather(city: str) -> str:
    """Returns weather info for the specified city."""
    return f"The weather in {city} is sunny"


@function_tool
def sum_numbers(a: int, b: int) -> int:
    """Adds two numbers."""
    return a + b


agent = Agent(
    name="Triage Agent",
    instructions="Get weather or sum numbers. You can use one tool at a time, switching to another tool in subsequent turns.",
    tools=[sum_numbers, get_weather],
)


async def main():
    try:
        result = await Runner.run(
            agent, "tell me karachi weather and sum 2+2 ans ", max_turns=1
        )
        print(result.final_output)
    except AgentsException as e:
        print(f"Caught AgentsException: {e}")


if __name__ == "__main__":
    asyncio.run(main())