import asyncio
import os
import shutil
from typing import Any

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServer, MCPServerStdio
from agents.mcp.util import MCPUtil, create_static_tool_filter
from agents.run_context import RunContextWrapper
from examples.auto_mode import confirm_with_fallback, is_auto_mode


async def list_tools(server: MCPServer, *, convert_to_strict: bool) -> list[Any]:
    """Fetch all MCP tools from the server."""

    run_context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    agent = Agent(name="ToolFetcher", instructions="Prefetch MCP tools.", mcp_servers=[server])

    return await MCPUtil.get_all_function_tools(
        [server],
        convert_schemas_to_strict=convert_to_strict,
        run_context=run_context,
        agent=agent,
    )


def prompt_user_approval(interruption_name: str) -> bool:
    """Ask the user to approve a tool call and return the decision."""
    if is_auto_mode():
        return confirm_with_fallback(
            f"Approve tool call '{interruption_name}'? (y/n): ",
            default=True,
        )
    while True:
        user_input = input(f"Approve tool call '{interruption_name}'? (y/n): ").strip().lower()
        if user_input == "y":
            return True
        if user_input == "n":
            return False
        print("Please enter 'y' or 'n'.")


async def resolve_interruptions(agent: Agent, result: Any) -> Any:
    """Prompt for approvals until no interruptions remain."""
    current_result = result
    while current_result.interruptions:
        state = current_result.to_state()
        # Human in the loop: prompt for approval on each tool call.
        for interruption in current_result.interruptions:
            if prompt_user_approval(interruption.name):
                print(f"Approving a tool call... (name: {interruption.name})")
                state.approve(interruption)
            else:
                print(f"Rejecting a tool call... (name: {interruption.name})")
                state.reject(interruption)
        current_result = await Runner.run(agent, state)
    return current_result


async def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    samples_dir = os.path.join(current_dir, "sample_files")
    blocked_path = os.path.join(samples_dir, "test.txt")

    async with MCPServerStdio(
        name="Filesystem Server",
        params={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
            "cwd": samples_dir,
        },
        require_approval={"always": {"tool_names": ["read_text_file"]}},
    ) as server:
        trace_id = gen_trace_id()
        with trace(workflow_name="MCP get_all_mcp_tools Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")

            print("=== Fetching all tools with strict schemas ===")
            all_tools = await list_tools(server, convert_to_strict=True)
            print(f"Found {len(all_tools)} tool(s):")
            for tool in all_tools:
                description = getattr(tool, "description", "") or ""
                print(f"- {tool.name}: {description}")

            # Build an agent that uses the prefetched tools instead of mcp_servers.
            prefetched_agent = Agent(
                name="Prefetched MCP Assistant",
                instructions=(
                    "Use the prefetched tools to help with file questions. "
                    "When using path arguments, prefer absolute paths in the allowed directory."
                ),
                tools=all_tools,
            )
            message = (
                f"List files in this allowed directory: {samples_dir}. "
                "Then read one of those files."
            )
            print(f"\nRunning: {message}\n")
            result = await Runner.run(prefetched_agent, message)
            result = await resolve_interruptions(prefetched_agent, result)
            print(result.final_output)

            # Apply a static tool filter and refetch tools.
            server.tool_filter = create_static_tool_filter(
                allowed_tool_names=["read_file", "list_directory"]
            )
            filtered_tools = await list_tools(server, convert_to_strict=False)

            print("\n=== After applying tool filter ===")
            print(f"Found {len(filtered_tools)} tool(s):")
            for tool in filtered_tools:
                print(f"- {tool.name}")

            filtered_agent = Agent(
                name="Filtered MCP Assistant",
                instructions=(
                    "Use the filtered tools to respond. "
                    "If a request requires a missing tool, explain that the capability is not "
                    "available."
                ),
                tools=filtered_tools,
            )
            blocked_message = (
                f'Create a file named "{blocked_path}" with the text "hello". '
                "If the available tools cannot create files, explain that clearly."
            )
            print(f"\nRunning: {blocked_message}\n")
            filtered_result = await Runner.run(filtered_agent, blocked_message)
            filtered_result = await resolve_interruptions(filtered_agent, filtered_result)
            print(filtered_result.final_output)


if __name__ == "__main__":
    if not shutil.which("npx"):
        raise RuntimeError("npx is required. Install it with `npm install -g npx`.")

    asyncio.run(main())
