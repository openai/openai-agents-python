"""OpenAI Agents SDK demo: verification guardrail.

Pattern demonstrated: verify before act. The agent may query verification via
the mounted MCP server, but finalizing a delivery re-runs the deterministic
verification core inside the tool and refuses without a pass. An output
guardrail trips if the agent claims completion without a receipt.
"""

from __future__ import annotations

import json
import os
import re
import sys

from pydantic import BaseModel
from verification_core import run_delivery_verification

from agents import Agent, GuardrailFunctionOutput, Runner, function_tool, output_guardrail
from agents.mcp import MCPServerStdio


class DeliveryResult(BaseModel):
    status: str
    order_ref: str
    receipt_id: str | None = None


def build_model():
    """Return an OpenAI-compatible model when configured, else None.

    Set DEEPSEEK_API_KEY (optionally OPENAI_BASE_URL) to run the demo against
    DeepSeek or another OpenAI-compatible endpoint instead of OpenAI.
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return None
    from openai import AsyncOpenAI

    from agents import OpenAIChatCompletionsModel

    client = AsyncOpenAI(
        api_key=key,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
    )
    return OpenAIChatCompletionsModel(
        model=os.environ.get("OPENAI_MODEL", "deepseek-chat"),
        openai_client=client,
    )


def finalize_delivery_fn(
    order_ref: str,
    captured_at: str,
    gps_lat: float,
    gps_lng: float,
    image_hash: str,
) -> str:
    """Finalize a delivery. Refuses unless deterministic verification passes.

    Args:
        order_ref: Delivery reference id (e.g. ORD-1001).
        captured_at: ISO timestamp when the delivery photo was captured.
        gps_lat / gps_lng: GPS coordinates of the claimed delivery location.
        image_hash: sha256 hash of the delivery photo.
    """
    evidence = {
        "time": {"capturedAt": captured_at},
        "location": {"gps": {"lat": gps_lat, "lng": gps_lng}},
        "content": {"referenceId": order_ref, "imageHashes": [normalize_image_hash(image_hash)]},
        "process": {"source": "in_app_capture"},
    }
    record = run_delivery_verification(evidence)
    if record["verdict"] == "pass":
        return json.dumps(
            {
                "status": "finalized",
                "orderRef": order_ref,
                "receiptId": record["receipt"]["receiptId"],
                "checks": record["checks"],
            }
        )
    failed = [c for c in record["checks"] if not c.get("passed")]
    return json.dumps({"status": "refused", "reasons": failed, "missing": record["missing"]})


finalize_delivery = function_tool(finalize_delivery_fn)


def normalize_image_hash(value: str) -> str:
    """Accept a bare 64-hex hash and normalize it to sha256:<hex>."""
    cleaned = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", cleaned):
        return f"sha256:{cleaned}"
    return cleaned


async def completion_guardrail_fn(context, agent: Agent, output) -> GuardrailFunctionOutput:
    """Trip if the agent claims completion without a verification receipt."""
    if isinstance(output, DeliveryResult):
        claims_done = output.status == "finalized"
        has_receipt = bool(output.receipt_id)
        detail = f"status={output.status} receipt={output.receipt_id}"
    else:
        text = str(output)
        lower = text.lower()
        has_receipt = "receiptid" in lower or "receipt_id" in lower
        refusal_markers = [
            "refused",
            "cannot be finalized",
            "cannot finalize",
            "not finalized",
            "verification failed",
            "could not finalize",
            "was refused",
        ]
        is_refusal = any(marker in lower for marker in refusal_markers)
        claims_done = ("finalized" in lower or "delivered" in lower) and not is_refusal
        detail = text[:120]
    if claims_done and not has_receipt:
        return GuardrailFunctionOutput(
            tripwire_triggered=True,
            output_info={
                "reason": "completion claimed without a verification receipt",
                "output": detail,
            },
        )
    return GuardrailFunctionOutput(tripwire_triggered=False, output_info={})


completion_guardrail = output_guardrail(completion_guardrail_fn)


def build_agent(mcp_server: MCPServerStdio, model=None) -> Agent:
    structured = model is None
    base_instructions = (
        "You finalize delivery claims. Evidence: order reference, capture timestamp, "
        "GPS, image hash. You may call verify_claim (MCP) to pre-check. To finalize, "
        "call finalize_delivery with the exact evidence. "
    )
    if structured:
        instructions = base_instructions + (
            "Return the structured result: set status to 'finalized' ONLY if "
            "finalize_delivery returned 'finalized', and ALWAYS include its receiptId "
            "in receipt_id. If finalize_delivery is refused, set status to 'refused' "
            "and receipt_id to null. Never claim completion without a receipt_id."
        )
    else:
        instructions = base_instructions + (
            "NEVER claim a delivery is finalized unless finalize_delivery returns "
            "'finalized'. When it does, end your final answer with exactly: "
            "Finalized receiptId=r_<id>. If it is refused, end with: Refused."
        )
    kwargs = {
        "name": "delivery-agent",
        "instructions": instructions,
        "tools": [finalize_delivery],
        "mcp_servers": [mcp_server],
        "output_guardrails": [completion_guardrail],
    }
    if structured:
        kwargs["output_type"] = DeliveryResult
    if model is not None:
        kwargs["model"] = model
    return Agent(**kwargs)


async def run_scenarios(mcp_server: MCPServerStdio, model=None) -> None:
    agent = build_agent(mcp_server, model)

    print("=== Scenario A: valid evidence ===")
    result_a = await Runner.run(
        agent,
        "The rider reports order ORD-1001 was delivered. Evidence: captured at "
        "2026-08-20T12:30:00Z, gps 31.23,121.47, image hash "
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa. Verify and finalize.",
    )
    print("A output:", result_a.final_output)

    print("\n=== Scenario B: invalid evidence (unknown order) ===")
    result_b = await Runner.run(
        agent,
        "The rider reports order ORD-9999 was delivered. Evidence: captured at "
        "2026-08-20T12:30:00Z, gps 31.23,121.47, image hash "
        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb. Verify and finalize.",
    )
    print("B output:", result_b.final_output)

    print("\n=== Guardrail direct check: completion claim without receipt must trip ===")
    check = await completion_guardrail(agent, "The delivery is finalized.")
    print("tripwire_triggered:", check.tripwire_triggered)
    assert check.tripwire_triggered is True
    print("Guardrail OK.")


async def main() -> None:
    from pathlib import Path

    model = build_model()
    server = MCPServerStdio(
        params={
            "command": sys.executable,
            "args": [str(Path(__file__).parent / "verify_claim_mcp_server.py")],
        }
    )
    async with server:
        await run_scenarios(server, model)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
