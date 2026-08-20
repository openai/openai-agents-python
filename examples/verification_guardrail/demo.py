"""Run the demo end to end.

Parts 1-3 (MCP verify, tool-level enforcement, guardrail) need no API key and
are deterministic. Part 4 runs the live OpenAI Agents SDK agent when a valid
OPENAI_API_KEY is available.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

VALID_EVIDENCE = {
    "time": {"capturedAt": "2026-08-20T12:30:00Z"},
    "location": {"gps": {"lat": 31.23, "lng": 121.47}},
    "content": {"referenceId": "ORD-1001", "imageHashes": [f"sha256:{'a' * 64}"]},
    "process": {"source": "in_app_capture"},
}
INVALID_EVIDENCE = {
    "time": {"capturedAt": "2026-08-20T12:30:00Z"},
    "location": {"gps": {"lat": 31.23, "lng": 121.47}},
    "content": {"referenceId": "ORD-9999", "imageHashes": [f"sha256:{'b' * 64}"]},
    "process": {"source": "in_app_capture"},
}

VALID_PROMPT = (
    "The rider reports order ORD-1001 was delivered. Evidence: captured at "
    "2026-08-20T12:30:00Z, gps 31.23,121.47, image hash "
    f"sha256:{'a' * 64}. Verify and finalize."
)
INVALID_PROMPT = (
    "The rider reports order ORD-9999 was delivered. Evidence: captured at "
    "2026-08-20T12:30:00Z, gps 31.23,121.47, image hash "
    f"sha256:{'b' * 64}. Verify and finalize."
)


async def part1_mcp() -> None:
    """Call verify_claim through the MCP server directly (no LLM, no key)."""
    print("=== Part 1: verify_claim via MCP server ===")
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).parent / "verify_claim_mcp_server.py")],
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for label, evidence in [("valid", VALID_EVIDENCE), ("invalid", INVALID_EVIDENCE)]:
                result = await session.call_tool(
                    "verify_claim",
                    {"claimType": "delivery_confirmed", "evidence": evidence},
                )
                text = result.content[0].text if result.content else "{}"
                record = json.loads(text)
                receipt = record.get("receipt", {}).get("receiptId", "no-receipt")
                print(f"  {label}: verdict={record.get('verdict')} receipt={receipt}")


def part2_tool() -> None:
    """finalize_delivery refuses unless deterministic verification passes."""
    print("=== Part 2: tool-level enforcement (finalize_delivery) ===")
    from agent import finalize_delivery_fn

    ok = json.loads(
        finalize_delivery_fn(
            "ORD-1001", "2026-08-20T12:30:00Z", 31.23, 121.47, f"sha256:{'a' * 64}"
        )
    )
    refused = json.loads(
        finalize_delivery_fn(
            "ORD-9999", "2026-08-20T12:30:00Z", 31.23, 121.47, f"sha256:{'b' * 64}"
        )
    )
    print(f"  valid evidence -> {ok['status']} receiptId={ok.get('receiptId', 'none')}")
    print(f"  invalid evidence -> {refused['status']} reasons={len(refused.get('reasons', []))}")


async def part3_guardrail() -> None:
    """Output guardrail trips when completion is claimed without a receipt."""
    print("=== Part 3: output guardrail (no receipt -> trip) ===")
    from agent import DeliveryResult, completion_guardrail_fn

    check = await completion_guardrail_fn(
        None, None, DeliveryResult(status="finalized", order_ref="ORD-1001")
    )
    print(f"  'finalized' without receiptId -> tripwire={check.tripwire_triggered}")
    assert check.tripwire_triggered is True
    check_ok = await completion_guardrail_fn(
        None, None, DeliveryResult(status="finalized", order_ref="ORD-1001", receipt_id="r_1")
    )
    print(f"  'finalized' with receiptId -> tripwire={check_ok.tripwire_triggered}")
    assert check_ok.tripwire_triggered is False


async def part4_live_agent() -> None:
    """Live OpenAI Agents SDK run (needs a valid OPENAI_API_KEY)."""
    print("=== Part 4: live OpenAI Agents SDK agent ===")
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("  skipped: OPENAI_API_KEY not set")
        return
    from agent import build_agent, build_model

    from agents import Runner
    from agents.mcp import MCPServerStdio

    server = MCPServerStdio(
        params={
            "command": sys.executable,
            "args": [str(Path(__file__).parent / "verify_claim_mcp_server.py")],
        }
    )
    try:
        async with server:
            agent = build_agent(server, build_model())
            result_a = await Runner.run(agent, VALID_PROMPT)
            print("  A (valid):", result_a.final_output)
            result_b = await Runner.run(agent, INVALID_PROMPT)
            print("  B (invalid):", result_b.final_output)
    except Exception as err:  # noqa: BLE001 - keep the demo runnable without a valid key
        print(f"  skipped: {type(err).__name__}: {str(err)[:160]}")


async def main() -> None:
    await part1_mcp()
    part2_tool()
    await part3_guardrail()
    await part4_live_agent()


if __name__ == "__main__":
    asyncio.run(main())
