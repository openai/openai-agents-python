import argparse
import asyncio

from agents import Agent, HostedMCPTool, ModelSettings, Runner, RunResult, RunResultStreaming

"""This example demonstrates how to use the hosted MCP support in the OpenAI Responses API against
the Ignav flight-search server (https://ignav.com/mcp), which exposes search_airports and
search_flights tools whose itineraries include a booking_url. The server is free to use
anonymously (rate-limited per IP), so no API key is required to run this example."""


async def main(verbose: bool, stream: bool):
    question = (
        "Find a one-way flight from SFO to JFK on 2026-07-15 and include a booking URL for the "
        "cheapest option."
    )
    agent = Agent(
        name="Assistant",
        instructions="You can use the Ignav hosted MCP server to search live flight prices.",
        model_settings=ModelSettings(tool_choice="required"),
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "ignav",
                    "server_url": "https://ignav.com/mcp",
                    "require_approval": "never",
                }
            )
        ],
    )

    run_result: RunResult | RunResultStreaming
    if stream:
        run_result = Runner.run_streamed(agent, question)
        async for event in run_result.stream_events():
            if event.type == "run_item_stream_event":
                print(f"Got event of type {event.item.__class__.__name__}")
        print(f"Done streaming; final result: {run_result.final_output}")
    else:
        run_result = await Runner.run(agent, question)
        print(run_result.final_output)
        # The cheapest one-way SFO -> JFK fare on 2026-07-15 is ... with this booking URL: ...

    if verbose:
        for item in run_result.new_items:
            print(item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream))
