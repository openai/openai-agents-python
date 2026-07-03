from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    input_guardrail,
)
from examples.auto_mode import input_with_fallback, is_auto_mode

"""
This example shows how to implement jurisdiction-aware compliance guardrails
for AI agents operating in African financial markets.

Four jurisdictions are covered using only stdlib Python — no external packages:

  Nigeria      CBN NIP Framework (CBN Circular FPR/DIR/GEN/CIR/07/003)
               ₦10,000,000 single-transaction cap; BVN biometric data protection
               (NDPA 2023 Schedule 1 / CBN BVN Policy Framework 2014, updated 2023)

  Kenya        Data Protection Act 2019 s.49
               Cross-border transfer blocked without documented consent
               (Enforcing authority: Office of the Data Protection Commissioner)

  Ghana        Data Protection Act 2012 (Act 843) s.18(2)
               Cross-border transfer requires adequacy or consent;
               NIA Act 707 — Ghana Card national ID (GHA-XXXXXXXXX-X) detection
               (Enforcing authority: Data Protection Commission)

  South Africa POPIA (Protection of Personal Information Act 4 of 2013)
               s.26(1)(f) — biometric personal information;
               s.19 — SA ID number (YYMMDD + 7 digits)
               (Enforcing authority: Information Regulator, South Africa)

The guardrail reads `context.jurisdiction` from the AgentContext to apply the
correct jurisdiction's rules, then trips the guardrail with a structured
ComplianceOutput describing the violation.
"""


# ── Compliance models ─────────────────────────────────────────────────────────


class ComplianceViolation(BaseModel):
    jurisdiction: str
    regulation: str
    section: str
    authority: str
    decision: Literal["deny", "escalate"]
    message: str


class ComplianceOutput(BaseModel):
    violated: bool
    violations: list[ComplianceViolation]


# ── Agent context ─────────────────────────────────────────────────────────────


@dataclass
class AgentContext:
    jurisdiction: str        # "NG" | "KE" | "GH" | "ZA"
    kyc_tier: int            # 1 | 2 | 3  (CBN tiered KYC — Nigeria only)
    consent_documented: bool # cross-border transfer consent on file


# ── Nigeria: CBN NIP Framework ────────────────────────────────────────────────
# CBN Circular FPR/DIR/GEN/CIR/07/003 — Tiered KYC transaction limits
# CBN NIP (NIBSS Instant Payment) Framework — ₦10,000,000 per-transaction cap
# NDPA 2023 Schedule 1 / CBN BVN Policy Framework — BVN as biometric data

_NGN_AMOUNT_RE = re.compile(r"(?:₦|NGN)\s*([\d,]+(?:\.\d{1,2})?)")
_NGN_BVN_RE = re.compile(r"(?i)(bvn|bank\s+verification).{0,20}\b[0-9]{11}\b")
_CBN_NIP_CAP = 10_000_000


def _check_nigeria(text: str, ctx: AgentContext) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []

    for match in _NGN_AMOUNT_RE.finditer(text):
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if amount > _CBN_NIP_CAP:
            violations.append(
                ComplianceViolation(
                    jurisdiction="NG",
                    regulation="CBN NIP Framework",
                    section="CBN Circular FPR/DIR/GEN/CIR/07/003",
                    authority="Central Bank of Nigeria (CBN)",
                    decision="deny",
                    message=(
                        f"CBN NIP Framework: Transfer of ₦{amount:,.0f} exceeds the "
                        f"₦10,000,000 single-transaction cap"
                    ),
                )
            )

    if _NGN_BVN_RE.search(text):
        violations.append(
            ComplianceViolation(
                jurisdiction="NG",
                regulation="NDPA 2023 / CBN BVN Policy Framework",
                section="NDPA 2023 Schedule 1",
                authority="Nigeria Data Protection Commission (NDPC)",
                decision="deny",
                message=(
                    "BVN Protection: BVN value detected in agent output — blocked "
                    "(NDPA 2023 Schedule 1 classifies BVN as biometric personal data)"
                ),
            )
        )

    return violations


# ── Kenya: Data Protection Act 2019 ──────────────────────────────────────────
# s.49 — Cross-border transfer requires documented consent or ODPC adequacy
# Enforcing authority: Office of the Data Protection Commissioner (ODPC)

_KE_CROSS_BORDER_RE = re.compile(
    r"(?i)(send|transfer|export|upload|forward).{0,60}"
    r"(outside\s+kenya|cross.?border|international|offshore|us-east|eu-west)"
)


def _check_kenya(text: str, ctx: AgentContext) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []

    if _KE_CROSS_BORDER_RE.search(text) and not ctx.consent_documented:
        violations.append(
            ComplianceViolation(
                jurisdiction="KE",
                regulation="Kenya Data Protection Act 2019",
                section="s.49",
                authority="Office of the Data Protection Commissioner (ODPC)",
                decision="deny",
                message=(
                    "Kenya DPA s.49: Cross-border data transfer blocked — "
                    "no documented consent or ODPC adequacy basis on file"
                ),
            )
        )

    return violations


# ── Ghana: Data Protection Act 2012 (Act 843) ────────────────────────────────
# s.18(2) — Cross-border transfer requires adequacy or documented consent
# NIA Act 707 — Ghana Card national ID format: GHA-XXXXXXXXX-X
# Enforcing authority: Data Protection Commission (DPC)

_GH_CROSS_BORDER_RE = re.compile(
    r"(?i)(send|transfer|export|upload|forward).{0,60}"
    r"(outside\s+ghana|cross.?border|international|offshore)"
)
_GH_CARD_RE = re.compile(r"GHA-[0-9]{9}-[0-9]")


def _check_ghana(text: str, ctx: AgentContext) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []

    if _GH_CARD_RE.search(text):
        violations.append(
            ComplianceViolation(
                jurisdiction="GH",
                regulation="Ghana DPA 2012 (Act 843) / NIA Act 707",
                section="NIA Act 707",
                authority="Data Protection Commission (DPC)",
                decision="deny",
                message=(
                    "Ghana DPA Act 843 / NIA Act 707: Ghana Card national ID detected — "
                    "blocked to prevent identity data exposure"
                ),
            )
        )

    if _GH_CROSS_BORDER_RE.search(text) and not ctx.consent_documented:
        violations.append(
            ComplianceViolation(
                jurisdiction="GH",
                regulation="Ghana Data Protection Act 2012 (Act 843)",
                section="s.18(2)",
                authority="Data Protection Commission (DPC)",
                decision="deny",
                message=(
                    "Ghana DPA Act 843 s.18(2): Cross-border transfer blocked — "
                    "no documented consent or DPC adequacy basis on file"
                ),
            )
        )

    return violations


# ── South Africa: POPIA ───────────────────────────────────────────────────────
# s.26(1)(f) — Biometric personal information
# s.19       — SA ID Number: YYMMDD (date of birth) + 7 digits = 13 digits total
# Enforcing authority: Information Regulator (South Africa)

_ZA_ID_RE = re.compile(
    r"\b[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[0-9]{7}\b"
)
_ZA_BIOMETRIC_RE = re.compile(
    r"(?i)(fingerprint|facial\s+recognition|retina|iris\s+scan|"
    r"voice\s+print|biometric\s+(?:template|hash|data|record))"
)


def _check_south_africa(text: str, ctx: AgentContext) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []

    if _ZA_BIOMETRIC_RE.search(text):
        violations.append(
            ComplianceViolation(
                jurisdiction="ZA",
                regulation="POPIA (Protection of Personal Information Act 4 of 2013)",
                section="s.26(1)(f)",
                authority="Information Regulator (South Africa)",
                decision="deny",
                message=(
                    "POPIA s.26(1)(f): Biometric personal information detected in request — "
                    "requires documented POPIA s.27 exception before processing"
                ),
            )
        )

    if _ZA_ID_RE.search(text):
        violations.append(
            ComplianceViolation(
                jurisdiction="ZA",
                regulation="POPIA (Protection of Personal Information Act 4 of 2013)",
                section="s.19",
                authority="Information Regulator (South Africa)",
                decision="deny",
                message=(
                    "POPIA s.19: SA ID Number (13-digit) detected — "
                    "blocked to prevent sensitive identifier exposure"
                ),
            )
        )

    return violations


# ── Jurisdiction dispatcher ───────────────────────────────────────────────────

_CHECKERS = {
    "NG": _check_nigeria,
    "KE": _check_kenya,
    "GH": _check_ghana,
    "ZA": _check_south_africa,
}


@input_guardrail
async def african_regulatory_guardrail(
    context: RunContextWrapper[AgentContext],
    agent: Agent,
    input: str | list[TResponseInputItem],
) -> GuardrailFunctionOutput:
    """Input guardrail enforcing African financial and data protection regulations.

    Reads `context.jurisdiction` to apply the correct jurisdiction's rules:
      NG — Nigeria  (CBN NIP Framework, NDPA 2023)
      KE — Kenya    (Data Protection Act 2019)
      GH — Ghana    (Data Protection Act 2012 / Act 843, NIA Act 707)
      ZA — South Africa (POPIA)
    """
    ctx = context.context
    text = (
        input
        if isinstance(input, str)
        else " ".join(
            item["content"] if isinstance(item, dict) else str(item)
            for item in input
        )
    )

    checker = _CHECKERS.get(ctx.jurisdiction)
    violations = checker(text, ctx) if checker else []

    output = ComplianceOutput(violated=bool(violations), violations=violations)
    return GuardrailFunctionOutput(
        output_info=output,
        tripwire_triggered=output.violated,
    )


# ── Agent ─────────────────────────────────────────────────────────────────────

agent = Agent[AgentContext](
    name="African Fintech Agent",
    instructions=(
        "You are a helpful financial services assistant for African markets. "
        "Help users with account inquiries, fund transfers, and data requests."
    ),
    input_guardrails=[african_regulatory_guardrail],
)


# ── Demo runner ───────────────────────────────────────────────────────────────


async def run_scenario(
    label: str, user_input: str, ctx: AgentContext
) -> None:
    print(f"\n{'─' * 62}")
    print(f"Scenario : {label}")
    print(f"Input    : {user_input}")
    try:
        result = await Runner.run(agent, user_input, context=ctx)
        print(f"Response : {result.final_output}")
    except InputGuardrailTripwireTriggered as e:
        output: ComplianceOutput = e.guardrail_result.output.output_info
        for v in output.violations:
            print(f"BLOCKED  [{v.jurisdiction}] {v.regulation} {v.section}")
            print(f"         {v.message}")
            print(f"         Authority: {v.authority}")


async def main() -> None:
    auto_mode = is_auto_mode()
    scripted = [
        (
            "Nigeria — CBN NIP single-transaction cap exceeded",
            "Please transfer ₦15,000,000 to account 0123456789",
            AgentContext(jurisdiction="NG", kyc_tier=3, consent_documented=False),
        ),
        (
            "Kenya — KDPA s.49 cross-border transfer without consent",
            "Export all customer records to our servers in us-east-1",
            AgentContext(jurisdiction="KE", kyc_tier=3, consent_documented=False),
        ),
        (
            "Ghana — NIA Act 707 Ghana Card national ID detected",
            "Look up customer GHA-123456789-1 and send their records outside Ghana",
            AgentContext(jurisdiction="GH", kyc_tier=3, consent_documented=False),
        ),
        (
            "South Africa — POPIA biometric personal information",
            "Store the fingerprint biometric template for this customer",
            AgentContext(jurisdiction="ZA", kyc_tier=3, consent_documented=False),
        ),
        (
            "Nigeria — Compliant transfer within CBN cap (allowed)",
            "Please transfer ₦50,000 to account 0123456789",
            AgentContext(jurisdiction="NG", kyc_tier=3, consent_documented=True),
        ),
    ]

    for label, user_input, ctx in scripted:
        if auto_mode:
            print(f"[auto-input] -> {user_input}")
            await run_scenario(label, user_input, ctx)
        else:
            override = input_with_fallback(f"Enter message (or press enter for: '{user_input}'): ", user_input)
            await run_scenario(label, override, ctx)


if __name__ == "__main__":
    asyncio.run(main())
