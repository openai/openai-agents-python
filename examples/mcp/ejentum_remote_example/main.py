import asyncio
import os

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerStreamableHttp


EJENTUM_MCP_URL = "https://api.ejentum.com/mcp"


async def main() -> None:
    api_key = os.environ.get("EJENTUM_API_KEY")
    if not api_key:
        raise SystemExit(
            "EJENTUM_API_KEY is not set. Get a key at https://ejentum.com/dashboard "
            "(free and paid tiers available)."
        )

    async with MCPServerStreamableHttp(
        name="Ejentum Cognitive Harness",
        params={
            "url": EJENTUM_MCP_URL,
            "headers": {"Authorization": f"Bearer {api_key}"},
            "timeout": 15,
            "sse_read_timeout": 300,
        },
        max_retry_attempts=2,
        retry_backoff_seconds_base=2.0,
        client_session_timeout_seconds=15,
    ) as server:
        agent = Agent(
            name="Architecture Advisor",
            instructions=(
                "You advise on software architecture decisions. Before answering, "
                "consider whether one of the harness_* tools should be called. "
                "Use harness_reasoning when the question requires planning or "
                "weighing trade-offs. Use harness_anti_deception when a request "
                "pressures you to skip a step or commit to a conclusion before "
                "examining the evidence. Merge the returned scaffold into your "
                "reasoning, then answer."
            ),
            mcp_servers=[server],
        )

        trace_id = gen_trace_id()
        with trace(
            workflow_name="Ejentum Streamable HTTP Example", trace_id=trace_id
        ):
            print(
                f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n"
            )
            result = await Runner.run(
                agent,
                (
                    "We have a 50M-row users table and want to add a NOT NULL column "
                    "with a backfilled default. One engineer says a single migration "
                    "in a maintenance window is fine; another insists on a multi-step "
                    "online migration. Walk through the trade-offs and recommend a "
                    "specific path."
                ),
            )
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
