"""Example: streaming progress events from long-running tools.

Demonstrates ToolContext.send_progress() — tools can emit intermediate
progress events that appear in stream_events() while the tool is still running.
"""

import asyncio

from agents import Agent, ItemHelpers, Runner, function_tool
from agents.stream_events import ToolProgressStreamEvent
from agents.tool_context import ToolContext


@function_tool
async def analyze_data(ctx: ToolContext, query: str) -> str:
    """Analyze data for a given query. Use this for complex analysis requests."""
    ctx.send_progress({"status": "starting", "query": query})
    await asyncio.sleep(1)

    ctx.send_progress({"status": "fetching_data", "progress": 0.25})
    await asyncio.sleep(1)

    ctx.send_progress({"status": "processing", "progress": 0.5})
    await asyncio.sleep(1)

    ctx.send_progress({"status": "finalizing", "progress": 1.0})
    await asyncio.sleep(0.5)

    return f"Analysis complete for '{query}': found 42 results with 95% confidence."


@function_tool
async def quick_lookup(ctx: ToolContext, term: str) -> str:
    """Look up a term quickly. Use this for simple lookups."""
    ctx.send_progress({"status": "searching", "term": term})
    await asyncio.sleep(0.5)
    return f"Found definition for '{term}': a common search term."


async def main():
    agent = Agent(
        name="Analyst",
        instructions=(
            "You are a data analyst. Use the analyze_data tool for complex queries "
            "and quick_lookup for simple lookups. Always use the tools when asked."
        ),
        tools=[analyze_data, quick_lookup],
    )

    print("Interactive tool progress streaming example.")
    print("Type a message to chat, or 'quit' to exit.\n")

    while True:
        user_input = input("You: ").strip()
        if not user_input or user_input.lower() == "quit":
            print("Goodbye!")
            break

        result = Runner.run_streamed(agent, input=user_input)
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                continue
            elif isinstance(event, ToolProgressStreamEvent):
                print(f"  [progress] {event.tool_name}: {event.data}")
            elif event.type == "agent_updated_stream_event":
                print(f"Agent: {event.new_agent.name}")
            elif event.type == "run_item_stream_event":
                if event.item.type == "tool_call_item":
                    print(f"\n-- Tool called: {getattr(event.item.raw_item, 'name', '?')}")
                elif event.item.type == "tool_call_output_item":
                    print(f"-- Tool output: {event.item.output}")
                elif event.item.type == "message_output_item":
                    print(f"\nAssistant: {ItemHelpers.text_message_output(event.item)}")

        print()


if __name__ == "__main__":
    asyncio.run(main())
