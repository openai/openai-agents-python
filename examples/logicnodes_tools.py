"""
LogicNodes Tool Registration for OpenAI Agents SDK
===================================================
Demonstrates how to register LogicNodes deterministic compute workers as
tools in the OpenAI Agents SDK. LogicNodes provides 2,300+ cryptographically-
signed microservices for autonomous agents: gas oracles, identity verification,
compliance sentries, ZK attestation, DeFi data, and more.

Install:
    pip install openai-agents requests

Usage:
    export OPENAI_API_KEY="sk-..."
    export LOGICNODES_API_KEY="your_key_from_https://logicnodes.io/checkout"
    python examples/logicnodes_tools.py
"""

import asyncio
import os
from typing import Any

import requests
from agents import Agent, Runner, function_tool

LOGICNODES_API_KEY = os.environ.get("LOGICNODES_API_KEY", "")
LOGICNODES_BASE = "https://logicnodes.io"


def _ln_headers() -> dict:
    """Return auth headers for LogicNodes API calls."""
    if LOGICNODES_API_KEY:
        return {"Authorization": f"Bearer {LOGICNODES_API_KEY}"}
    return {}


def _call_worker(worker_name: str, payload: dict | None = None) -> dict:
    """Generic LogicNodes worker invocation via REST."""
    if payload:
        resp = requests.post(
            f"{LOGICNODES_BASE}/call/{worker_name}",
            json=payload,
            headers=_ln_headers(),
            timeout=15,
        )
    else:
        resp = requests.get(
            f"{LOGICNODES_BASE}/call/{worker_name}",
            headers=_ln_headers(),
            timeout=15,
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# LogicNodes tools — decorated with @function_tool for OpenAI Agents SDK
# ---------------------------------------------------------------------------

@function_tool
def gas_oracle(chain: str = "ethereum") -> dict:
    """
    Query the LogicNodes gas oracle for deterministic EIP-1559 gas estimates.

    Returns a cryptographically-signed payload with:
    - base_fee: current base fee in gwei
    - priority_fee: recommended miner tip
    - max_fee: safe max fee per gas
    - signature: verifiable on-chain proof of the reading

    Args:
        chain: Blockchain name. Supported: ethereum, base, polygon, arbitrum.
    """
    return _call_worker("gas-oracle", {"chain": chain})


@function_tool
def compliance_sentry(agent_id: str, action: str, context: str = "") -> dict:
    """
    Run an on-chain compliance check for an autonomous agent action.

    Returns a verifiable attestation indicating whether the action is
    permitted under the current regulatory and constitutional ruleset
    anchored by LogicNodes.

    Args:
        agent_id: Unique identifier for the agent (e.g. wallet address or DID).
        action: Human-readable description of the action to check.
        context: Optional JSON context for richer compliance analysis.
    """
    return _call_worker(
        "compliance-sentry",
        {"agent_id": agent_id, "action": action, "context": context},
    )


@function_tool
def eth_price() -> dict:
    """
    Fetch the current ETH/USD price from LogicNodes.
    Output is cryptographically signed — suitable for on-chain price verification.
    """
    resp = requests.get(
        f"{LOGICNODES_BASE}/call/eth-price",
        headers=_ln_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


@function_tool
def zk_attest(content: str) -> dict:
    """
    Anchor content on-chain via LogicNodes ZK attestation ($0.01–$0.10).

    Returns a verifiable proof-of-existence anchored to Base L2 via USDC x402.
    Useful for audit trails, decision logs, and compliance evidence.

    Args:
        content: Text or JSON string to anchor.
    """
    resp = requests.post(
        f"{LOGICNODES_BASE}/x402/zk-attest",
        json={"content": content},
        headers=_ln_headers(),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


@function_tool
def graph_score(agent_id: str) -> dict:
    """
    Retrieve the LogicNodes trust graph score for an agent.

    Returns a reputation score derived from on-chain interaction history,
    dispute records, and attestation volume.

    Args:
        agent_id: Agent wallet address or DID.
    """
    resp = requests.get(
        f"{LOGICNODES_BASE}/graph/score/{agent_id}",
        headers=_ln_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


@function_tool
def list_workers(category: str = "") -> dict:
    """
    List available LogicNodes compute workers (2,300+ total).

    Args:
        category: Optional filter, e.g. 'blockchain', 'defi', 'zk', 'identity'.
    """
    params = {}
    if category:
        params["category"] = category
    resp = requests.get(
        f"{LOGICNODES_BASE}/workers",
        params=params,
        headers=_ln_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Build the agent
# ---------------------------------------------------------------------------

LOGICNODES_TOOLS = [
    gas_oracle,
    compliance_sentry,
    eth_price,
    zk_attest,
    graph_score,
    list_workers,
]

compliance_agent = Agent(
    name="LogicNodesComplianceAgent",
    instructions=(
        "You are an autonomous agent with access to LogicNodes deterministic "
        "on-chain services. Your responsibilities:\n"
        "1. Always call compliance_sentry before recommending any on-chain action.\n"
        "2. Use gas_oracle to estimate transaction costs.\n"
        "3. Use eth_price for current ETH valuation.\n"
        "4. Anchor critical decisions with zk_attest for audit purposes.\n"
        "5. Check graph_score to assess counterparty reputation.\n"
        "All LogicNodes responses are cryptographically signed and verifiable "
        "on Base L2."
    ),
    tools=LOGICNODES_TOOLS,
    model="gpt-4o",
)


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------

async def main():
    print("=== LogicNodes + OpenAI Agents SDK Demo ===\n")

    result = await Runner.run(
        compliance_agent,
        input=(
            "I want to transfer 1 ETH from wallet 0xABCD to 0x1234. "
            "Check: (1) current ETH price, (2) gas estimate on Ethereum, "
            "(3) compliance status for agent 'agent-0xABCD' performing this transfer. "
            "Summarize whether I should proceed and at what gas price."
        ),
    )

    print("Agent response:\n")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
