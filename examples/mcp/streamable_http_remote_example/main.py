import asyncio

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerStreamableHttp


async def main():
    async with MCPServerStreamableHttp(
        name="DeepWiki MCP Streamable HTTP Server",
        params={
            "url": "https://mcp.deepwiki.com/mcp",
            # Allow more time for remote tool responses.
            "timeout": 15,
            "sse_read_timeout": 300,
        },
        # Retry slow/unstable remote calls a couple of times.
        max_retry_attempts=2,
        retry_backoff_seconds_base=2.0,
        client_session_timeout_seconds=15,
    ) as server:
        agent = Agent(
            name="DeepWiki Assistant",
            instructions="Use the tools to respond to user requests.",
            mcp_servers=[server],
        )

        trace_id = gen_trace_id()
        with trace(workflow_name="DeepWiki Streamable HTTP Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
            result = await Runner.run(
                agent,
                "For the repository openai/codex, tell me the primary programming language.",
            )
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
