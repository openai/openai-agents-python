import asyncio
import os

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerStreamableHttp


HASHLOCK_MCP_URL = "https://mcp.hashlock.markets/mcp"


async def main():
    token = os.environ.get("HASHLOCK_ACCESS_TOKEN")
    if not token:
        raise SystemExit(
            "Missing HASHLOCK_ACCESS_TOKEN. "
            "Get one at https://hashlock.markets/mcp/auth (SIWE wallet signature)."
        )

    async with MCPServerStreamableHttp(
        name="HashLock OTC Streamable HTTP Server",
        params={
            "url": HASHLOCK_MCP_URL,
            "headers": {"Authorization": f"Bearer {token}"},
            "timeout": 15,
            "sse_read_timeout": 300,
        },
        # Remote quote polling can be slow when market makers are busy.
        max_retry_attempts=2,
        retry_backoff_seconds_base=2.0,
        client_session_timeout_seconds=15,
    ) as server:
        agent = Agent(
            name="OTC Quote Assistant",
            instructions=(
                "You help users request OTC crypto quotes via HashLock. "
                "Use create_rfq to post a quote request, then poll with "
                "get_rfq until quotes arrive or the RFQ expires. "
                "NEVER trigger on-chain HTLC writes — those require the user "
                "to sign in their own wallet at hashlock.markets."
            ),
            mcp_servers=[server],
        )

        trace_id = gen_trace_id()
        with trace(workflow_name="HashLock OTC Streamable HTTP Example", trace_id=trace_id):
            print(
                f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n"
            )
            result = await Runner.run(
                agent,
                "I want to sell 0.5 ETH for USDC. What's the best quote?",
            )
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
