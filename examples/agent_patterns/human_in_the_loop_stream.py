"""Human-in-the-loop example with streaming.

This example demonstrates the human-in-the-loop (HITL) pattern with streaming.
The agent will pause execution when a tool requiring approval is called,
allowing you to approve or reject the tool call before continuing.

The streaming version provides real-time feedback as the agent processes
the request, then pauses for approval when needed.
"""

import asyncio

from agents import Agent, Runner, function_tool
from examples.auto_mode import confirm_with_fallback


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


@function_tool
async def get_weather(city: str) -> str:
    """Get the weather for a given city.

    Args:
        city: The city to get weather for.

    Returns:
        Weather information for the city.
    """
    return f"The weather in {city} is sunny."


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
    main_agent = Agent(
        name="Weather Assistant",
        instructions=(
            "You are a helpful weather assistant. "
            "Answer questions about weather and temperature using the available tools."
        ),
        tools=[get_temperature, get_weather],
    )

    # Run the agent with streaming
    result = Runner.run_streamed(
        main_agent,
        "What is the weather and temperature in Oakland?",
    )
    async for _ in result.stream_events():
        pass  # Process streaming events silently or could print them

    # Handle interruptions
    while len(result.interruptions) > 0:
        print("\n" + "=" * 80)
        print("Human-in-the-loop: approval required for the following tool calls:")
        print("=" * 80)

        state = result.to_state()

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

        # Resume execution with streaming
        print("\nResuming agent execution...")
        result = Runner.run_streamed(main_agent, state)
        async for _ in result.stream_events():
            pass  # Process streaming events silently or could print them

    print("\n" + "=" * 80)
    print("Final Output:")
    print("=" * 80)
    print(result.final_output)
    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
