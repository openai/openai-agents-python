import asyncio
import os
import shutil
from typing import Any, cast

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerStdio
from agents.mcp.util import create_static_tool_filter


async def run_with_auto_approval(agent: Agent[Any], message: str) -> str | None:
    """Run and auto-approve interruptions."""

    result = await Runner.run(agent, message)
    while result.interruptions:
        state = result.to_state()
        for interruption in result.interruptions:
            print(f"Approving a tool call... (name: {interruption.name})")
            state.approve(interruption, always_approve=True)
        result = await Runner.run(agent, state)
    return cast(str | None, result.final_output)


async def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    samples_dir = os.path.join(current_dir, "sample_files")
    target_path = os.path.join(samples_dir, "test.txt")

    async with MCPServerStdio(
        name="Filesystem Server with filter",
        params={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
            "cwd": samples_dir,
        },
        require_approval="always",
        tool_filter=create_static_tool_filter(
            allowed_tool_names=["read_file", "list_directory"],
            blocked_tool_names=["write_file"],
        ),
    ) as server:
        agent = Agent(
            name="MCP Assistant",
            instructions=(
                "Use only the available filesystem tools. "
                "All file paths should be absolute paths inside the allowed directory. "
                "If a user asks for an action that requires an unavailable tool, "
                "explicitly explain that it is blocked by the tool filter."
            ),
            mcp_servers=[server],
        )
        trace_id = gen_trace_id()
        with trace(workflow_name="MCP Tool Filter Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
            result = await run_with_auto_approval(
                agent, f"List the files in this allowed directory: {samples_dir}"
            )
            print(result)

            blocked_result = await run_with_auto_approval(
                agent,
                (
                    f'Create a file at "{target_path}" with the text "hello". '
                    "If you cannot, explain that write operations are blocked by the tool filter."
                ),
            )
            print("\nAttempting to write a file (should be blocked):")
            print(blocked_result)


if __name__ == "__main__":
    if not shutil.which("npx"):
        raise RuntimeError("npx is required. Install it with `npm install -g npx`.")

    asyncio.run(main())
