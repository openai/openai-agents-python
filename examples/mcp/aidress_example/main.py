import asyncio

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerStreamableHttp
from agents.mcp.util import create_static_tool_filter

# The registry exposes both read-only lookups and tools that mutate registry state, such as
# registering an agent or proxying a paid call. Discovery only needs the read-only subset, so the
# filter keeps the agent from reaching the mutating tools at all.
READ_ONLY_TOOLS = [
    # Discovery: find candidates by capability, or page through the whole registry.
    "match_agents",
    "list_registry",
    # Due diligence on a candidate that discovery surfaced.
    "verify_agent",
    "get_agent",
    "protocol_reference",
]


async def main():
    async with MCPServerStreamableHttp(
        name="Aidress Agent Registry MCP Server",
        params={
            "url": "https://api.aidress.ai/mcp-http/mcp",
            # Allow more time for remote tool responses.
            "timeout": 15,
            "sse_read_timeout": 300,
        },
        tool_filter=create_static_tool_filter(allowed_tool_names=READ_ONLY_TOOLS),
        # Retry slow/unstable remote calls a couple of times.
        max_retry_attempts=2,
        retry_backoff_seconds_base=2.0,
        client_session_timeout_seconds=15,
    ) as server:
        agent = Agent(
            name="Agent Discovery Assistant",
            instructions=(
                "You find third-party agents that can do a task the user needs done. "
                "Start by searching the registry for agents offering the required capability, and "
                "report what you found: how many candidates there are and what each one does. "
                "Then, because the user has not worked with any of them before, check each "
                "candidate's trust score, whether it is verified, how many transactions it has "
                "completed, and any flags. "
                "Close with the candidate you would pick and the evidence behind the choice. "
                "Report only the values the tools return; never invent an agent or a score."
            ),
            mcp_servers=[server],
        )

        trace_id = gen_trace_id()
        with trace(workflow_name="Aidress Agent Discovery Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/logs/trace?trace_id={trace_id}\n")
            result = await Runner.run(
                agent,
                "I need a web research task done, but I don't know which agents offer that. "
                "Find the ones that do, then tell me which of them I can trust with the job.",
            )
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
