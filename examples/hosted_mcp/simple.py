import argparse
import asyncio

from agents import Agent, HostedMCPTool, ModelSettings, Runner, RunResult, RunResultStreaming

"""This example demonstrates how to use the hosted MCP support in the OpenAI Responses API, with
approvals not required for any tools. You should only use this for trusted MCP servers."""


async def main(verbose: bool, stream: bool, repo: str):
    question = f"Which language is the repository {repo} written in?"
    agent = Agent(
        name="Assistant",
        instructions=f"You can use the hosted MCP server to inspect {repo}.",
        model_settings=ModelSettings(tool_choice="required"),
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
        # The repository is primarily written in multiple languages, including Rust and TypeScript...

    if verbose:
        for item in run_result.new_items:
            print(item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    parser.add_argument(
        "--repo",
        default="https://github.com/openai/openai-agents-python",
        help="Repository URL or slug that the Git MCP server should use.",
    )
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream, args.repo))
