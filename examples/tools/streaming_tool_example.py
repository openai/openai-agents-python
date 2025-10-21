"""
Example of using streaming tools with the Agents SDK.

This example demonstrates how to create a tool that yields incremental output,
allowing you to stream tool execution results to the user in real-time.
"""

import asyncio
from collections.abc import AsyncIterator

from agents import Agent, Runner, ToolOutputStreamEvent, function_tool


@function_tool
async def search_documents(query: str) -> AsyncIterator[str]:
    """Search through documents and stream results as they are found.

    Args:
        query: The search query.

    Yields:
        Incremental search results.
    """
    # Simulate searching through multiple documents
    documents = [
        f"Document 1 contains information about {query}...\n",
        f"Document 2 has additional details on {query}...\n",
        f"Document 3 provides analysis of {query}...\n",
    ]

    for doc in documents:
        # Simulate processing time
        await asyncio.sleep(0.5)
        # Yield incremental results
        yield doc


@function_tool
async def generate_report(topic: str) -> AsyncIterator[str]:
    """Generate a report on a topic, streaming the output as it's generated.

    Args:
        topic: The topic to generate a report on.

    Yields:
        Incremental report content.
    """
    sections = [
        f"# Report on {topic}\n\n",
        f"## Introduction\n\nThis report covers {topic} in detail.\n\n",
        f"## Analysis\n\nOur analysis of {topic} shows several key points...\n\n",
        f"## Conclusion\n\nIn summary, {topic} is an important topic.\n\n",
    ]

    for section in sections:
        await asyncio.sleep(0.3)
        yield section


async def main():
    # Create an agent with streaming tools
    agent = Agent(
        name="Research Assistant",
        instructions="You are a helpful research assistant that can search documents and generate reports.",
        tools=[search_documents, generate_report],
    )

    # Run the agent in streaming mode
    result = Runner.run_streamed(
        agent,
        input="Search for information about artificial intelligence and generate a brief report.",
    )

    print("Streaming agent output:\n")

    # Stream events and display tool outputs in real-time
    async for event in result.stream_events():
        # Handle tool streaming events
        if event.type == "tool_output_stream_event":
            assert isinstance(event, ToolOutputStreamEvent)
            print(f"[{event.tool_name}] {event.delta}", end="", flush=True)

        # Handle run item events (final outputs)
        elif event.type == "run_item_stream_event":
            if event.name == "tool_output":
                print(f"\n✓ Tool '{event.item.agent.name}' completed\n")
            elif event.name == "message_output_created":
                print(f"\n[Agent Response]: {event.item}\n")

    # Get final result
    print("\n" + "=" * 60)
    print("Final output:", result.final_output)
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
