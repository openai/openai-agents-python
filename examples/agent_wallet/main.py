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
# Define tools that call the paid API
# ---------------------------------------------------------------------------

@function_tool
def get_market_data(symbol: str, credential: str) -> str:
    """Get market data for a cryptocurrency symbol.

    This is a paid API call that requires agent authorization.
    The credential proves the agent has permission to spend on data access.

    Args:
        symbol: Cryptocurrency symbol (BTC, ETH, SOL).
        credential: Agent's authorization credential.
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
            "to look up cryptocurrency prices. Always pass the credential "
            "provided in context.\n\n"
            f"Your credential: {authorized_credential}"
        ),
        tools=[get_market_data],
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
            "You are a monitoring agent. Try to use get_market_data to check "
            "BTC price. Pass the credential provided in context.\n\n"
            f"Your credential: {readonly_credential}"
        ),
        tools=[get_market_data],
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
