"""Agent Wallet Example — authorized agent payments with OpenAI Agents SDK.

Demonstrates the agent wallet pattern: an agent proves it has the right
authorization from its operator before making paid API calls.

Run: uv run python examples/agent_wallet/main.py
"""

import asyncio
import os
import sys
import time

# Ensure sibling modules are importable when run from repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents import Agent, Runner, function_tool, gen_trace_id, trace
from mock_server import call_paid_api
from wallet import Credential


# ---------------------------------------------------------------------------
# Create credentials for two agents with different permissions
# ---------------------------------------------------------------------------

# Agent 1: authorized for paid data access
authorized_credential = Credential(
    agent_id="research-agent-001",
    operator_id="operator-acme-corp",
    permissions={"read_data", "financial_small"},
    expiry=time.time() + 3600,  # 1 hour
).to_json()

# Agent 2: read-only, NOT authorized for financial operations
readonly_credential = Credential(
    agent_id="monitor-agent-002",
    operator_id="operator-acme-corp",
    permissions={"read_data"},
    expiry=time.time() + 3600,
).to_json()


# ---------------------------------------------------------------------------
# Tool factory — credential is bound via closure, NOT passed by the model
# ---------------------------------------------------------------------------

def make_market_data_tool(credential: str):
    """Create a market data tool with a credential bound in the closure.

    The credential is supplied by trusted application code at agent setup
    time, not by the model at runtime. This prevents the model from
    fabricating or escalating credentials.
    """

    @function_tool
    def get_market_data(symbol: str) -> str:
        """Get market data for a cryptocurrency symbol.

        This is a paid API call. Authorization is handled automatically
        via the agent's bound credential.

        Args:
            symbol: Cryptocurrency symbol (BTC, ETH, SOL).
        """
        result = call_paid_api(credential=credential, symbol=symbol)
        if result.get("error"):
            return f"Error: {result['message']}"
        data = result["data"]
        return (
            f"{result['symbol']}: ${data['price']:,.2f} "
            f"({data['change_24h']:+.1f}% 24h) "
            f"[authorized as {result['authorized_agent']}]"
        )

    return get_market_data


# ---------------------------------------------------------------------------
# Run two scenarios
# ---------------------------------------------------------------------------

async def run():
    # Scenario 1: Authorized agent
    print("=" * 60)
    print("Scenario 1: Authorized agent (research-agent-001)")
    print("  Permissions: read_data, financial_small")
    print("=" * 60)

    authorized_agent = Agent(
        name="ResearchAgent",
        instructions=(
            "You are a market research agent. Use the get_market_data tool "
            "to look up cryptocurrency prices."
        ),
        tools=[make_market_data_tool(authorized_credential)],
    )

    result = await Runner.run(
        starting_agent=authorized_agent,
        input="Get the current price of BTC and ETH.",
    )
    print(f"\nResult: {result.final_output}\n")

    # Scenario 2: Unauthorized agent (lacks financial_small)
    print("=" * 60)
    print("Scenario 2: Unauthorized agent (monitor-agent-002)")
    print("  Permissions: read_data only (no financial_small)")
    print("=" * 60)

    unauthorized_agent = Agent(
        name="MonitorAgent",
        instructions=(
            "You are a monitoring agent. Use get_market_data to check "
            "the BTC price."
        ),
        tools=[make_market_data_tool(readonly_credential)],
    )

    result = await Runner.run(
        starting_agent=unauthorized_agent,
        input="Get the current BTC price.",
    )
    print(f"\nResult: {result.final_output}\n")


if __name__ == "__main__":
    trace_id = gen_trace_id()
    print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
    with trace("Agent Wallet Example", trace_id=trace_id):
        asyncio.run(run())
