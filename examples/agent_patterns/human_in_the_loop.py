"""Human-in-the-loop example with tool approval.

This example demonstrates how to:
1. Define tools that require approval before execution
2. Handle interruptions when tool approval is needed
3. Serialize/deserialize run state to continue execution later
4. Approve or reject tool calls based on user input
"""

import asyncio
import json
from pathlib import Path

from agents import Agent, Runner, RunState, function_tool
from examples.auto_mode import confirm_with_fallback


@function_tool
async def get_weather(city: str) -> str:
    """Get the weather for a given city.

    Args:
        city: The city to get weather for.

    Returns:
        Weather information for the city.
    """
    return f"The weather in {city} is sunny"


async def _needs_temperature_approval(_ctx, params, _call_id) -> bool:
    """Check if temperature tool needs approval."""
    return "Oakland" in params.get("city", "")


@function_tool(
    # Dynamic approval: only require approval for Oakland
    needs_approval=_needs_temperature_approval
)
async def get_temperature(city: str) -> str:
    """Get the temperature for a given city.

    Args:
        city: The city to get temperature for.

    Returns:
        Temperature information for the city.
    """
    return f"The temperature in {city} is 20° Celsius"


# Main agent with tool that requires approval
agent = Agent(
    name="Weather Assistant",
    instructions=(
        "You are a helpful weather assistant. "
        "Answer questions about weather and temperature using the available tools."
    ),
    tools=[get_weather, get_temperature],
)

RESULT_PATH = Path(".cache/agent_patterns/human_in_the_loop/result.json")


async def confirm(question: str) -> bool:
    """Prompt user for yes/no confirmation.

    Args:
        question: The question to ask.

    Returns:
        True if user confirms, False otherwise.
    """
    return confirm_with_fallback(f"{question} (y/n): ", default=True)


async def main():
    """Run the human-in-the-loop example."""
    result = await Runner.run(
        agent,
        "What is the weather and temperature in Oakland?",
    )

    has_interruptions = len(result.interruptions) > 0

    while has_interruptions:
        print("\n" + "=" * 80)
        print("Run interrupted - tool approval required")
        print("=" * 80)

        # Storing state to file (demonstrating serialization)
        state = result.to_state()
        state_json = state.to_json()
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RESULT_PATH.open("w") as f:
            json.dump(state_json, f, indent=2)

        print(f"State saved to {RESULT_PATH}")

        # From here on you could run things on a different thread/process

        # Reading state from file (demonstrating deserialization)
        print(f"Loading state from {RESULT_PATH}")
        with RESULT_PATH.open() as f:
            stored_state_json = json.load(f)

        state = await RunState.from_json(agent, stored_state_json)

        # Process each interruption
        for interruption in result.interruptions:
            print("\nTool call details:")
            print(f"  Agent: {interruption.agent.name}")
            print(f"  Tool: {interruption.name}")
            print(f"  Arguments: {interruption.arguments}")

            confirmed = await confirm("\nDo you approve this tool call?")

            if confirmed:
                print(f"✓ Approved: {interruption.name}")
                state.approve(interruption)
            else:
                print(f"✗ Rejected: {interruption.name}")
                state.reject(interruption)

        # Resume execution with the updated state
        print("\nResuming agent execution...")
        result = await Runner.run(agent, state)
        has_interruptions = len(result.interruptions) > 0

    print("\n" + "=" * 80)
    print("Final Output:")
    print("=" * 80)
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
