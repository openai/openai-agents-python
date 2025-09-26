import argparse
import asyncio

from agents import (
    Agent,
    HostedMCPTool,
    MCPToolApprovalFunctionResult,
    MCPToolApprovalRequest,
    Runner,
)

"""This example demonstrates how to use the hosted MCP support in the OpenAI Responses API, with
approval callbacks."""


def approval_callback(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
    answer = input(f"Approve running the tool `{request.data.name}`? (y/n) ")
    result: MCPToolApprovalFunctionResult = {"approve": answer == "y"}
    if not result["approve"]:
        result["reason"] = "User denied"
    return result


async def main(verbose: bool, stream: bool):
    agent = Agent(
        name="Assistant",
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "gitmcp",
                    "server_url": "https://gitmcp.io/openai/codex",
                    "require_approval": "always",
                },
                on_approval_request=approval_callback,
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

    if verbose:
        for item in res.new_items:
            print(item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream))
