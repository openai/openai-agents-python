import argparse
import asyncio

from agents import Agent, HostedMCPTool, Runner

"""This example demonstrates how to use the hosted MCP support in the OpenAI Responses API, with
approvals not required for any tools. You should only use this for trusted MCP servers."""


async def main(verbose: bool, stream: bool):
    agent = Agent(
        name="Assistant",
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "gitmcp",
                    "server_url": "https://gitmcp.io/openai/codex",
                    "require_approval": "never",
                }
            )
        ],
    )

    if stream:
        result = Runner.run_streamed(agent, "Which language is this repo written in?")
        async for event in result.stream_events():
            if event.type == "run_item_stream_event":
                print(f"Got event of type {event.item.__class__.__name__}")
        print(f"Done streaming; final result: {result.final_output}")
    else:
        res = await Runner.run(
            agent,
            "Which language is this repo written in? Your MCP server should know what the repo is.",
        )
        print(res.final_output)
        # The repository is primarily written in multiple languages, including Rust and TypeScript...

    if verbose:
        for item in res.new_items:
            print(item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream))
