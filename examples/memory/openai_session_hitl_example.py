"""
Example demonstrating OpenAI Conversations session with human-in-the-loop (HITL) tool approval.

This example shows how to use OpenAI Conversations session memory combined with
human-in-the-loop tool approval. The session maintains conversation history while
requiring approval for specific tool calls.
"""

import asyncio

from agents import Agent, OpenAIConversationsSession, Runner, function_tool
from examples.auto_mode import confirm_with_fallback, input_with_fallback, is_auto_mode


async def _needs_approval(_ctx, _params, _call_id) -> bool:
    """Always require approval for weather tool."""
    return True


@function_tool(needs_approval=_needs_approval)
def get_weather(location: str) -> str:
    """Get weather for a location.

    Args:
        location: The location to get weather for

    Returns:
        Weather information as a string
    """
    # Simulated weather data
    weather_data = {
        "san francisco": "Foggy, 58°F",
        "oakland": "Sunny, 72°F",
        "new york": "Rainy, 65°F",
    }
    # Check if any city name is in the provided location string
    location_lower = location.lower()
    for city, weather in weather_data.items():
        if city in location_lower:
            return weather
    return f"Weather data not available for {location}"


async def prompt_yes_no(question: str) -> bool:
    """Prompt user for yes/no answer.

    Args:
        question: The question to ask

    Returns:
        True if user answered yes, False otherwise
    """
    return confirm_with_fallback(f"\n{question} (y/n): ", default=True)


async def main():
    # Create an agent with a tool that requires approval
    agent = Agent(
        name="HITL Assistant",
        instructions="You help users with information. Always use available tools when appropriate. Keep responses concise.",
        tools=[get_weather],
    )

    # Create a session instance that will persist across runs
    session = OpenAIConversationsSession()

    print("=== OpenAI Session + HITL Example ===")
    print("Enter a message to chat with the agent. Submit an empty line to exit.")
    print("The agent will ask for approval before using tools.\n")

    auto_mode = is_auto_mode()

    while True:
        # Get user input
        if auto_mode:
            user_message = input_with_fallback("You: ", "What's the weather in Oakland?")
        else:
            print("You: ", end="", flush=True)
            loop = asyncio.get_event_loop()
            user_message = await loop.run_in_executor(None, input)

        if not user_message.strip():
            break

        # Run the agent
        result = await Runner.run(agent, user_message, session=session)

        # Handle interruptions (tool approvals)
        while result.interruptions:
            # Get the run state
            state = result.to_state()

            for interruption in result.interruptions:
                tool_name = interruption.name or "Unknown tool"
                args = interruption.arguments or "(no arguments)"

                approved = await prompt_yes_no(
                    f"Agent {interruption.agent.name} wants to call '{tool_name}' with {args}. Approve?"
                )

                if approved:
                    state.approve(interruption)
                    print("Approved tool call.")
                else:
                    state.reject(interruption)
                    print("Rejected tool call.")

            # Resume the run with the updated state
            result = await Runner.run(agent, state, session=session)

        # Display the response
        reply = result.final_output or "[No final output produced]"
        print(f"Assistant: {reply}\n")
        if auto_mode:
            break


if __name__ == "__main__":
    asyncio.run(main())
