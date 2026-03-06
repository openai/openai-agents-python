import asyncio

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerSse


async def main():
    async with MCPServerSse(
        name="GitMCP SSE Server",
        params={"url": "https://gitmcp.io/openai/codex"},
    ) as server:
        agent = Agent(
            name="SSE Assistant",
            instructions="Use the available MCP tools to help the user.",
            mcp_servers=[server],
        )

        trace_id = gen_trace_id()
        with trace(workflow_name="SSE MCP Server Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
            result = await Runner.run(agent, "Please help me with the available tools.")
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
