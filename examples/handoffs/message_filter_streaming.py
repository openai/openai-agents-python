from __future__ import annotations

import json
import random

from agents import Agent, HandoffInputData, Runner, function_tool, handoff, trace
from agents.extensions import handoff_filters


@function_tool
def random_number_tool(max: int) -> int:
    """Return a random integer between 0 and the given maximum."""
    return random.randint(0, max)


def spanish_handoff_message_filter(handoff_message_data: HandoffInputData) -> HandoffInputData:
    # First, we'll remove any tool-related messages from the message history
    handoff_message_data = handoff_filters.remove_all_tools(handoff_message_data)

    # Second, we'll also remove the first two items from the history, just for demonstration
    history = (
        tuple(handoff_message_data.input_history[2:])
        if isinstance(handoff_message_data.input_history, tuple)
        else handoff_message_data.input_history
    )

    # or, you can use the HandoffInputData.clone(kwargs) method
    return HandoffInputData(
        input_history=history,
        pre_handoff_items=tuple(handoff_message_data.pre_handoff_items),
        new_items=tuple(handoff_message_data.new_items),
    )


first_agent = Agent(
    name="Assistant",
    instructions="Be extremely concise.",
    tools=[random_number_tool],
)

spanish_agent = Agent(
    name="Spanish Assistant",
    instructions="You only speak Spanish and are extremely concise.",
    handoff_description="A Spanish-speaking assistant.",
)

second_agent = Agent(
    name="Assistant",
    instructions=(
        "Be a helpful assistant. If the user speaks Spanish, handoff to the Spanish assistant."
    ),
    handoffs=[handoff(spanish_agent, input_filter=spanish_handoff_message_filter)],
)


async def main():
    # Trace the entire run as a single workflow
    with trace(workflow_name="Streaming message filter"):
        # 1. Send a regular message to the first agent
        result = await Runner.run(first_agent, input="Hi, my name is Sora.")

        print("Step 1 done")

        # 2. Ask it to generate a number
        result = await Runner.run(
            first_agent,
            input=result.to_input_list()
            + [{"content": "Can you generate a random number between 0 and 100?", "role": "user"}],
        )

        print("Step 2 done")

        # 3. Call the second agent
        result = await Runner.run(
            second_agent,
            input=result.to_input_list()
            + [
                {
                    "content": "I live in New York City. Whats the population of the city?",
                    "role": "user",
                }
            ],
        )

        print("Step 3 done")

        # 4. Cause a handoff to occur
        stream_result = Runner.run_streamed(
            second_agent,
            input=result.to_input_list()
            + [
                {
                    "content": "Por favor habla en español. ¿Cuál es mi nombre y dónde vivo?",
                    "role": "user",
                }
            ],
        )
        async for _ in stream_result.stream_events():
            pass

        print("Step 4 done")

    print("\n===Final messages===\n")

    # 5. That should have caused spanish_handoff_message_filter to be called, which means the
    # output should be missing the first two messages, and have no tool calls.
    # Let's print the messages to see what happened
    for item in stream_result.to_input_list():
        print(json.dumps(item, indent=2))
        """
        $python examples/handoffs/message_filter_streaming.py
        Step 1 done
        Step 2 done
        Step 3 done
        Tu nombre y lugar de residencia no los tengo disponibles. Solo sé que mencionaste vivir en la ciudad de Nueva York.
        Step 4 done

        ===Final messages===

        {
            "content": "Can you generate a random number between 0 and 100?",
            "role": "user"
            }
            {
            "id": "...",
            "content": [
                {
                "annotations": [],
                "text": "Sure! Here's a random number between 0 and 100: **37**.",
                "type": "output_text"
                }
            ],
            "role": "assistant",
            "status": "completed",
            "type": "message"
            }
            {
            "content": "I live in New York City. Whats the population of the city?",
            "role": "user"
            }
            {
            "id": "...",
            "content": [
                {
                "annotations": [],
                "text": "As of the latest estimates, New York City's population is approximately 8.5 million people. Would you like more information about the city?",
                "type": "output_text"
                }
            ],
            "role": "assistant",
            "status": "completed",
            "type": "message"
            }
            {
            "content": "Por favor habla en espa\u00f1ol. \u00bfCu\u00e1l es mi nombre y d\u00f3nde vivo?",
            "role": "user"
            }
            {
            "id": "...",
            "content": [
                {
                "annotations": [],
                "text": "No s\u00e9 tu nombre, pero me dijiste que vives en Nueva York.",
                "type": "output_text"
                }
            ],
            "role": "assistant",
            "status": "completed",
            "type": "message"
            }
        """


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
