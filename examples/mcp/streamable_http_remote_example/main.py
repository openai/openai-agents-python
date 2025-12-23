import asyncio

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerStreamableHttp


async def main():
    async with MCPServerStreamableHttp(
        name="GitMCP Streamable HTTP Server",
        params={"url": "https://gitmcp.io/openai/codex"},
    ) as server:
        agent = Agent(
            name="GitMCP Assistant",
            instructions="Use the tools to respond to user requests.",
            mcp_servers=[server],
        )

        trace_id = gen_trace_id()
        with trace(workflow_name="GitMCP Streamable HTTP Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
            result = await Runner.run(
                agent,
                "Which language is this repo written in? The MCP server knows which repo to investigate.",
            )
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
