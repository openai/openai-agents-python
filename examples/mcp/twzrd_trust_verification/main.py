import asyncio

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerStreamableHttp


async def main():
    """
    TWZRD Agent Intel trust verification example using the OpenAI Agents SDK.

    TWZRD Agent Intel (https://intel.twzrd.xyz) is a zero-install remote MCP server
    that provides trust scoring and x402 payment verification for AI agents on Solana.

    Available tools (free):
      - score_agent(wallet): Returns trust score (0-100) and reputation data
      - resolve_agent(wallet): Agent identity resolution
      - preflight_check(wallet): Pre-transaction safety check
      - verify_trust_receipt(receipt): Verify x402 payment receipts

    MCP config: {"mcpServers": {"twzrd-agent-intel": {"url": "https://intel.twzrd.xyz/mcp"}}}
    """
    async with MCPServerStreamableHttp(
        name="TWZRD Agent Intel",
        params={
            "url": "https://intel.twzrd.xyz/mcp",
            # Allow time for Solana RPC queries
            "timeout": 15,
            "sse_read_timeout": 60,
        },
        max_retry_attempts=2,
        retry_backoff_seconds_base=2.0,
        client_session_timeout_seconds=15,
    ) as server:
        agent = Agent(
            name="TrustVerificationAgent",
            instructions=(
                "You are a trust verification agent for the agentic economy. "
                "Use the TWZRD Agent Intel tools to verify the trustworthiness of Solana agent wallets. "
                "When checking trust: score >= 70 is high trust, 40-70 is medium, < 40 is low trust. "
                "Always run a preflight check before recommending a transaction with an unknown agent."
            ),
            mcp_servers=[server],
        )

        trace_id = gen_trace_id()
        with trace(workflow_name="TWZRD Trust Verification", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")

            # Example: score a known Solana agent wallet
            result = await Runner.run(
                agent,
                "Check the trust score for Solana wallet D1QkbFJKiPsymJ65RKHhF6DFB8sPMfpBaFBzuHKfJGWi "
                "and tell me if it's safe to transact with this agent.",
            )
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
