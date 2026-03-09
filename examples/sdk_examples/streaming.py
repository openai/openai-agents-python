"""Streaming response examples.

Demonstrates how to stream agent responses, handle different stream event types,
and process streaming results with tools and handoffs.

Setup:
    export OPENAI_API_KEY="your-api-key"

Usage:
    python examples/sdk_examples/streaming.py
"""

import asyncio

from openai.types.responses import ResponseTextDeltaEvent

from agents import Agent, ItemHelpers, Runner, function_tool


# --- Example 1: Basic Text Streaming ---


async def example_text_streaming() -> None:
    """Stream raw text deltas as the agent generates its response."""
    agent = Agent(
        name="Storyteller",
        instructions="You tell very short stories (3-4 sentences max).",
    )

    print("[Text Stream] ", end="")
    result = Runner.run_streamed(agent, input="Tell me a very short story about a robot.")
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)
    print()  # Newline after streaming.


# --- Example 2: Stream Events with Items ---


@function_tool
def lookup_fact(topic: str) -> str:
    """Look up a fun fact about a topic."""
    facts = {
        "sun": "The Sun accounts for 99.86% of the mass in the solar system.",
        "ocean": "The ocean contains about 20 million tons of gold.",
        "moon": "The Moon is slowly drifting away from Earth at about 3.8 cm per year.",
    }
    return facts.get(topic.lower(), f"Fun fact: {topic} is interesting!")


async def example_item_streaming() -> None:
    """Stream high-level SDK events including tool calls and messages."""
    agent = Agent(
        name="Fact Bot",
        instructions="Look up a fun fact about the topic the user mentions.",
        tools=[lookup_fact],
    )

    result = Runner.run_streamed(agent, input="Tell me a fun fact about the sun.")
    async for event in result.stream_events():
        if event.type == "raw_response_event":
            # Skip raw deltas in this example.
            continue
        elif event.type == "agent_updated_stream_event":
            print(f"[Item Stream] Agent: {event.new_agent.name}")
        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                print(
                    f"[Item Stream] Tool called: "
                    f"{getattr(event.item.raw_item, 'name', 'unknown')}"
                )
            elif event.item.type == "tool_call_output_item":
                print(f"[Item Stream] Tool output: {event.item.output}")
            elif event.item.type == "message_output_item":
                text = ItemHelpers.text_message_output(event.item)
                print(f"[Item Stream] Message: {text}")


# --- Example 3: Streaming with Handoffs ---


async def example_streaming_handoff() -> None:
    """Stream events across agent handoffs to see agent transitions."""
    joke_agent = Agent(
        name="Joke Agent",
        handoff_description="Tells jokes",
        instructions="Tell a short joke and nothing else.",
    )

    router_agent = Agent(
        name="Router",
        instructions="If the user asks for a joke, hand off to the joke agent.",
        handoffs=[joke_agent],
    )

    result = Runner.run_streamed(router_agent, input="Tell me a joke.")
    current_agent = "Router"
    async for event in result.stream_events():
        if event.type == "agent_updated_stream_event":
            current_agent = event.new_agent.name
            print(f"[Handoff Stream] Switched to: {current_agent}")
        elif event.type == "run_item_stream_event":
            if event.item.type == "handoff_call_item":
                print(f"[Handoff Stream] Handoff initiated from {current_agent}")
            elif event.item.type == "message_output_item":
                text = ItemHelpers.text_message_output(event.item)
                if text:
                    print(f"[Handoff Stream] [{current_agent}] {text}")


# --- Example 4: Collecting Final Result from Stream ---


async def example_stream_final_result() -> None:
    """Consume the stream and access the final result afterward."""
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant. Be concise.",
    )

    result = Runner.run_streamed(agent, input="What are the 3 primary colors?")

    # Consume all events to drive the stream to completion.
    char_count = 0
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            char_count += len(event.data.delta)

    # Access the final result after streaming is complete.
    print(f"[Final Result] Output: {result.final_output}")
    print(f"[Final Result] Characters streamed: {char_count}")
    print(f"[Final Result] Items generated: {len(result.new_items)}")
    print(f"[Final Result] Last agent: {result.last_agent.name}")


# --- Example 5: Streaming Multiple Tool Calls ---


@function_tool
def get_population(city: str) -> str:
    """Get the population of a city."""
    populations = {
        "tokyo": "13.96 million",
        "london": "8.98 million",
        "new york": "8.34 million",
    }
    return populations.get(city.lower(), "Unknown")


@function_tool
def get_area(city: str) -> str:
    """Get the area of a city in square kilometers."""
    areas = {
        "tokyo": "2,194 km2",
        "london": "1,572 km2",
        "new york": "783 km2",
    }
    return areas.get(city.lower(), "Unknown")


async def example_multi_tool_streaming() -> None:
    """Stream events when the agent makes multiple tool calls."""
    agent = Agent(
        name="City Info",
        instructions=(
            "Look up city information using the available tools. "
            "Use both tools to give a complete answer."
        ),
        tools=[get_population, get_area],
    )

    result = Runner.run_streamed(agent, input="What are the population and area of Tokyo?")
    tool_calls_seen = 0
    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                tool_calls_seen += 1
                print(
                    f"[Multi-Tool] Tool call #{tool_calls_seen}: "
                    f"{getattr(event.item.raw_item, 'name', 'unknown')}"
                )
            elif event.item.type == "tool_call_output_item":
                print(f"[Multi-Tool] Tool result: {event.item.output}")
            elif event.item.type == "message_output_item":
                text = ItemHelpers.text_message_output(event.item)
                if text:
                    print(f"[Multi-Tool] Final: {text}")


# --- Run all examples ---


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Streaming Examples")
    print("=" * 60)

    examples = [
        ("1. Basic Text Streaming", example_text_streaming),
        ("2. Item Stream Events", example_item_streaming),
        ("3. Streaming with Handoffs", example_streaming_handoff),
        ("4. Collecting Final Result", example_stream_final_result),
        ("5. Multi-Tool Streaming", example_multi_tool_streaming),
    ]

    for title, example_fn in examples:
        print(f"\n--- {title} ---")
        await example_fn()


if __name__ == "__main__":
    asyncio.run(main())
