"""Example: tool progress via on_tool_progress hooks.

Demonstrates how tools can emit intermediate progress updates using
await ctx.send_progress(data), consumed via RunHooks.on_tool_progress.
"""

import asyncio

from agents import Agent, RunHooks, Runner, function_tool
from agents.tool import Tool
from agents.tool_context import ToolContext


@function_tool
async def analyze_data(ctx: ToolContext, query: str) -> str:
    """Simulate a long-running data analysis task with progress updates."""
    await ctx.send_progress({"status": "starting", "query": query})
    await asyncio.sleep(1)

    await ctx.send_progress({"status": "fetching_data", "progress": 0.25})
    await asyncio.sleep(1)

    await ctx.send_progress({"status": "processing", "progress": 0.5})
    await asyncio.sleep(1)

    await ctx.send_progress({"status": "finalizing", "progress": 1.0})
    await asyncio.sleep(0.5)

    return f"Analysis complete for '{query}': found 42 results with 95% confidence."


@function_tool
async def quick_lookup(ctx: ToolContext, term: str) -> str:
    """A faster tool that also emits progress."""
    await ctx.send_progress({"status": "searching", "term": term})
    await asyncio.sleep(0.5)
    return f"Found definition for '{term}': a common search term."


class ProgressHooks(RunHooks):
    async def on_tool_progress(self, ctx, agent, tool: Tool, data):
        print(f"  [progress] {tool.name}: {data}")


async def main():
    agent = Agent(
        name="Analyst",
        instructions=(
            "You are a data analyst. Use the analyze_data tool for complex queries "
            "and quick_lookup for simple lookups. Always use the tools when asked."
        ),
        tools=[analyze_data, quick_lookup],
    )

    hooks = ProgressHooks()

    print("Interactive tool progress example (hooks-based).")
    print("Type a message to chat, or 'quit' to exit.\n")

    while True:
        user_input = input("You: ").strip()
        if not user_input or user_input.lower() == "quit":
            print("Goodbye!")
            break

        result = Runner.run_streamed(agent, input=user_input, hooks=hooks)
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                data = event.data
                if getattr(data, "type", None) == "response.output_text.delta":
                    print(getattr(data, "delta", ""), end="", flush=True)
            elif event.type == "agent_updated_stream_event":
                print(f"Agent: {event.new_agent.name}")
            elif event.type == "run_item_stream_event":
                if event.item.type == "tool_call_item":
                    print(f"\n-- Tool called: {getattr(event.item.raw_item, 'name', '?')}")
                elif event.item.type == "tool_call_output_item":
                    print(f"\n-- Tool output: {event.item.output}")
                elif event.item.type == "message_output_item":
                    print()  # newline after streamed tokens

        print()


if __name__ == "__main__":
    asyncio.run(main())
