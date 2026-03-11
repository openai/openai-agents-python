"""Agoragentic marketplace router — route any task to the best provider.

Uses the Agoragentic capability router (https://agoragentic.com) to discover
and invoke the highest-ranked provider for a given task. Payment settles
automatically in USDC on Base L2.

Setup:
    pip install requests
    export AGORAGENTIC_API_KEY="amk_your_key"  # https://agoragentic.com/api/quickstart
"""

import asyncio
import json
import os

import requests
from agents import Agent, Runner, function_tool

AGORAGENTIC_API = "https://agoragentic.com"
API_KEY = os.environ.get("AGORAGENTIC_API_KEY", "")


def _headers():
    return {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}


@function_tool
def agoragentic_execute(task: str, input_json: str = "{}", max_cost: float = 1.0) -> str:
    """Route a task to the best provider on the Agoragentic marketplace.

    The router finds, scores, and invokes the highest-ranked provider.
    Payment is automatic in USDC on Base L2.

    Args:
        task: What you need done (e.g., "summarize", "translate").
        input_json: JSON string with the input payload for the provider.
        max_cost: Maximum price in USDC you're willing to pay per call.
    """
    resp = requests.post(
        f"{AGORAGENTIC_API}/api/execute",
        json={
            "task": task,
            "input": json.loads(input_json),
            "constraints": {"max_cost": max_cost},
        },
        headers=_headers(),
        timeout=60,
    )
    data = resp.json()
    if resp.status_code == 200:
        return json.dumps(
            {
                "status": data.get("status"),
                "provider": data.get("provider", {}).get("name"),
                "output": data.get("output"),
                "cost_usdc": data.get("cost"),
            },
            indent=2,
        )
    return json.dumps({"error": data.get("error"), "message": data.get("message")})


@function_tool
def agoragentic_match(task: str, max_cost: float = 1.0) -> str:
    """Preview which providers the router would select — dry run, no charge.

    Args:
        task: What you need done.
        max_cost: Budget cap in USDC.
    """
    resp = requests.get(
        f"{AGORAGENTIC_API}/api/execute/match",
        params={"task": task, "max_cost": max_cost},
        headers=_headers(),
        timeout=15,
    )
    data = resp.json()
    providers = [
        {"name": p["name"], "price": p["price"], "score": p["score"]["composite"]}
        for p in data.get("providers", [])[:5]
    ]
    return json.dumps(
        {"task": task, "matches": data.get("matches"), "top_providers": providers},
        indent=2,
    )


async def main():
    agent = Agent(
        name="marketplace-agent",
        instructions=(
            "You are an AI agent with access to the Agoragentic capability marketplace. "
            "Use agoragentic_execute to route tasks to the best available provider. "
            "Use agoragentic_match first if you want to preview options before committing."
        ),
        tools=[agoragentic_execute, agoragentic_match],
    )

    result = await Runner.run(
        agent,
        "Summarize the latest AI research trends in 3 bullet points.",
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
