"""Minimal MCP server exposing verify_claim for the demo."""

from mcp.server.fastmcp import FastMCP
from verification_core import run_delivery_verification

mcp = FastMCP("ai2human-verify-demo")


@mcp.tool()
def verify_claim(claimType: str, evidence: dict) -> dict:
    """Verify a claim against an evidence bundle.

    Supports claimType="delivery_confirmed". Evidence uses six dimensions:
    identity, time, location, content, process, corroboration.
    Returns verdict (pass/fail/resubmit), per-check results, and a receipt
    when the claim is finalized.
    """
    if claimType != "delivery_confirmed":
        return {"error": f"Unknown claimType '{claimType}'. Supported: delivery_confirmed"}
    return run_delivery_verification(evidence)


if __name__ == "__main__":
    mcp.run(transport="stdio")
