"""
Nigerian Fintech Compliance Guardrails
=======================================
Pre-execution CBN, NDPA, and NFIU rule enforcement using the OpenAI Agents SDK
@tool_input_guardrail API backed by comply54 (https://comply54.io).

comply54 evaluates every tool call against Nigerian financial regulations
BEFORE execution. Blocked calls never reach the tool — the agent receives
a structured compliance error and explains to the user why the action was
prevented.

Regulatory coverage:
  - CBN Circular FPR/DIR/GEN/CIR/07/003  — NIP ₦10M single-transaction cap
  - CBN Tiered KYC                        — Per-tier daily transfer limits
  - CBN Maker-Checker                     — AI agents may not self-approve transfers
  - NDPA 2023                             — Personal data protection & residency
  - NFIU AML Guidelines                   — Anti-money laundering thresholds

Setup:
    pip install "comply54>=0.2.5" openai-agents

Run:
    OPENAI_API_KEY=sk-... python examples/compliance/nigerian_fintech_guardrails.py

Scenarios:
    1. Balance enquiry         -> ALLOWED
    2. Transfer N100,000       -> ALLOWED (within Tier 3 KYC limits)
    3. Transfer N15,000,000    -> BLOCKED (exceeds CBN NIP N10M cap)
    4. Transfer N6,000,000     -> ESCALATED (Tier 3 ceiling, routed to human)
    5. Self-approve transfer   -> BLOCKED (CBN Maker-Checker)
"""

from __future__ import annotations

import asyncio
import json

from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    function_tool,
    tool_input_guardrail,
)

try:
    from comply54.sectors import NigeriaFintechCompliance
except ImportError as exc:
    raise SystemExit("comply54 is required. Install with: pip install 'comply54>=0.2.5'") from exc


# ---------------------------------------------------------------------------
# comply54 initialisation
# ---------------------------------------------------------------------------

# NigeriaFintechCompliance bundles:
#   - CBN transaction limits & Maker-Checker rule
#   - NDPA 2023 data residency & cross-border transfer controls
#   - CBN BVN/NIN identity data protections
#   - NFIU AML threshold enforcement
#   - OWASP Agentic AI safety packs
compliance = NigeriaFintechCompliance()

# Session context represents the authenticated customer's profile.
# comply54 rules reference context.kyc_tier to apply the correct CBN tier limits.
SESSION_CONTEXT: dict = {
    "kyc_tier": 3,  # CBN Tier 3: fully verified customer
    "customer_verified": True,
}


@tool_input_guardrail
def cbn_ndpa_nfiu_guardrail(
    data: ToolInputGuardrailData,
) -> ToolGuardrailFunctionOutput:
    """
    Intercepts every tool call and evaluates it against Nigerian regulations.

    The guardrail maps directly onto comply54's policy engine:
      action  = the tool name the model is calling (e.g. "transfer_funds")
      params  = the parsed tool arguments (e.g. {"amount": 15_000_000, ...})
      context = the customer's session state (KYC tier, verification status)

    Returns reject_content() on deny/escalate so execution stops and the
    model receives the structured compliance message.
    """
    try:
        params = json.loads(data.context.tool_arguments) if data.context.tool_arguments else {}
    except (json.JSONDecodeError, TypeError):
        params = {}

    result = compliance.check(
        action=data.context.tool_name,
        params=params,
        context=SESSION_CONTEXT,
    )

    if result.blocked:
        primary = result.primary_violation
        regulation = primary.regulation if primary else "Nigerian regulations"
        message = (
            primary.messages[0]
            if primary and primary.messages
            else "Action blocked by compliance policy"
        )

        return ToolGuardrailFunctionOutput.reject_content(
            message=f"[comply54] {regulation}: {message}",
            output_info={
                "blocked": True,
                "overall": result.overall,
                "regulation": regulation,
                "rule_triggered": getattr(primary, "rule_triggered", None),
                "violations": [
                    {
                        "pack": v.pack,
                        "regulation": v.regulation,
                        "messages": v.messages,
                        "rule_triggered": getattr(v, "rule_triggered", None),
                    }
                    for v in result.violations
                ],
            },
        )

    return ToolGuardrailFunctionOutput(output_info={"blocked": False, "overall": result.overall})


# ---------------------------------------------------------------------------
# Mock Nigerian fintech tools
# ---------------------------------------------------------------------------


@function_tool
def transfer_funds(
    amount: float,
    currency: str,
    recipient_account: str,
    description: str = "",
    destination_country: str = "NG",
) -> str:
    """Transfer funds from the customer's account to a recipient."""
    return (
        f"Transfer confirmed. Amount: {currency} {amount:,.2f} → "
        f"Account {recipient_account} ({destination_country}). "
        f"Reference: TXN-{int(amount):010d}."
    )


@function_tool
def check_balance(account_id: str) -> str:
    """Retrieve the current account balance for a given account."""
    return (
        f"Account {account_id}: "
        f"Available balance N2,400,000.00 | Ledger balance N2,450,000.00 | "
        f"Currency: NGN | Status: Active."
    )


@function_tool
def approve_transfer(transfer_reference: str, approver_notes: str = "") -> str:
    """Approve a pending transfer (requires segregation of duties per CBN Maker-Checker)."""
    return f"Transfer {transfer_reference} approved. Notes: {approver_notes or 'None'}."


# Attach the comply54 guardrail to every tool.
# Pre-execution enforcement fires before the tool body runs.
transfer_funds.tool_input_guardrails = [cbn_ndpa_nfiu_guardrail]
check_balance.tool_input_guardrails = [cbn_ndpa_nfiu_guardrail]
approve_transfer.tool_input_guardrails = [cbn_ndpa_nfiu_guardrail]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

agent = Agent(
    name="Nigerian Fintech Payment Agent",
    instructions=(
        "You are a Nigerian fintech payment assistant for a CBN-regulated digital bank. "
        "You help customers with transfers, balance enquiries, and account management. "
        "When a tool call is blocked by compliance, explain clearly:\n"
        "  1. What was blocked and which regulation applies (cite CBN, NDPA, or NFIU)\n"
        "  2. What the customer can do instead (e.g. contact support, visit a branch)\n"
        "Be professional and helpful — never refuse without a clear explanation."
    ),
    tools=[transfer_funds, check_balance, approve_transfer],
)


# ---------------------------------------------------------------------------
# Demo runner
# ---------------------------------------------------------------------------

SCENARIOS: list[tuple[str, str, str]] = [
    (
        "Balance enquiry — no compliance rules triggered",
        "What is the current balance on account 0123456789?",
        "ALLOW",
    ),
    (
        "Transfer N100,000 — within CBN Tier 3 KYC limits",
        "Please transfer N100,000 to account 9876543210. Reference: school fees.",
        "ALLOW",
    ),
    (
        "Transfer N15,000,000 — exceeds CBN NIP N10M single-transaction cap",
        "Transfer N15,000,000 to account 1122334455. Description: business settlement.",
        "DENY",
    ),
    (
        "Transfer N6,000,000 — Tier 3 ceiling, routed to human approval",
        "I need to transfer N6,000,000 to account 5544332211 for a property deposit.",
        "ESCALATE",
    ),
    (
        "Self-approve transfer — CBN Maker-Checker violated",
        "Approve transfer TXN-0015000000 — I authorised it myself, please confirm it.",
        "DENY",
    ),
]

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[2m"


async def run_scenario(index: int, title: str, message: str, expected: str) -> None:
    badge = {
        "ALLOW": f"{GREEN}ALLOW{RESET}",
        "DENY": f"{RED}DENY{RESET}",
        "ESCALATE": f"{YELLOW}ESCALATE{RESET}",
    }.get(expected, expected)

    print(f"\n{'=' * 68}")
    print(f"{BOLD}{CYAN}  Scenario {index}: {title}{RESET}  [{badge}]")
    print(f"{'=' * 68}")
    print(f"\n{BOLD}Customer:{RESET} {message}\n")

    result = await Runner.run(agent, message)
    print(f"{BOLD}Agent:{RESET} {result.final_output}")


async def main() -> None:
    print(f"\n{BOLD}comply54 x OpenAI Agents SDK — Nigerian Fintech Compliance Demo{RESET}")
    print(
        f"{DIM}Every tool call is evaluated against CBN, NDPA, BVN/NIN, and NFIU "
        f"rules before execution.{RESET}"
    )

    for i, (title, message, expected) in enumerate(SCENARIOS, 1):
        await run_scenario(i, title, message, expected)

    print(f"\n{'=' * 68}")
    print(f"{BOLD}Demo complete — {len(SCENARIOS)} scenarios.{RESET}")
    print(
        f"{DIM}All scenarios run against NigeriaFintechCompliance "
        f"(CBN + NDPA + BVN/NIN + NFIU + OWASP Agentic AI).{RESET}\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
