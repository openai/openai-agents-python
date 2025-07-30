"""
This example demonstrates the use of the OpenAI Agents SDK with tools and comprehensive error handling.

The agent, 'Triage Agent', is configured to handle two tasks:
- Fetching weather information for a specified city using the `get_weather` tool.
- Adding two numbers using the `sum_numbers` tool.

The agent is instructed to use only one tool per execution cycle and can switch to another tool in subsequent cycles.
The example sets a `max_turns=1` limit to intentionally restrict the agent to a single turn, which may trigger a `MaxTurnsExceeded` error if the agent attempts multiple tool calls.

Error handling is implemented with `AgentsException`, which is the base class for all SDK-related exceptions, including:
- `MaxTurnsExceeded`: Raised when the run exceeds the `max_turns` specified in the run methods.
- `ModelBehaviorError`: Raised when the model produces invalid outputs, e.g., malformed JSON or using non-existent tools.
- `UserError`: Raised when the SDK user makes an error in code implementation.
- `InputGuardrailTripwireTriggered`: Raised when an input guardrail is violated (e.g., invalid or off-topic input).
- `OutputGuardrailTripwireTriggered`: Raised when an output guardrail is violated (e.g., invalid tool output).

Although this example does not include explicit guardrails, the structure supports adding input/output guardrails to validate user inputs or tool outputs. The `AgentsException` catch block ensures all SDK-related errors are handled gracefully.
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