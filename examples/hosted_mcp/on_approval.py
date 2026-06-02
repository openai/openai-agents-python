import argparse
import asyncio
import json
from typing import Literal

from agents import (
    Agent,
    HostedMCPTool,
    MCPToolApprovalFunctionResult,
    MCPToolApprovalRequest,
    Runner,
    RunResult,
    RunResultStreaming,
)
from examples.auto_mode import confirm_with_fallback


def prompt_approval(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
    params: object = request.data.arguments or {}
    approved = confirm_with_fallback(
        f"Approve running tool (mcp: {request.data.name}, params: {json.dumps(params)})? (y/n) ",
        default=True,
    )
    result: MCPToolApprovalFunctionResult = {"approve": approved}
    if not approved:
        result["reason"] = "User denied"
    return result


async def main(verbose: bool, stream: bool) -> None:
    require_approval: Literal["always"] = "always"
    agent = Agent(
        name="MCP Assistant",
        instructions=(
            "You must always use the MCP tools to answer questions. "
            "Use the DeepWiki hosted MCP server to answer questions and do not ask the user for "
            "additional configuration."
        ),
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "deepwiki",
                    "server_url": "https://mcp.deepwiki.com/mcp",
                    "require_approval": require_approval,
                },
                on_approval_request=prompt_approval,
            )
        ],
    )

    question = "Which language is the repository openai/codex written in?"

    run_result: RunResult | RunResultStreaming
    if stream:
        run_result = Runner.run_streamed(agent, question)
        async for event in run_result.stream_events():
            if verbose:
                print(event)
            elif (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                print(event.data.delta, end="", flush=True)
        if not verbose:
            print()
        print(f"Done streaming; final result: {run_result.final_output}")
    else:
        run_result = await Runner.run(agent, question)
        while run_result.interruptions:
            run_result = await Runner.run(agent, run_result.to_state())
        print(run_result.final_output)

    if verbose:
        for item in run_result.new_items:
            print(item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream))
