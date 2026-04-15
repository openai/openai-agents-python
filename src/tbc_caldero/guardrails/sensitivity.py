"""
Sensitivity Guardrail — unified P1-P6 confidentiality gate.

Consolidates 3 hooks from the vault into one parametrized guardrail:
- .claude/hooks/discord-sensitivity-gate.py
- .claude/hooks/critical-action-gate.sh (partial: Slack/Gmail sensitivity)
- .claude/hooks/financial-gate.sh (content-level sensitivity)

Migration source: staging/2026-04-15-hooks-migration-inventory.md (Capa C.1)
Canonical rule: .claude/rules/external-comms-confidentiality.md

P1-P6 categories (from the rule):
- P1: CEO evaluates a person negatively
- P2: CEO sees organizational gap/weakness
- P3: Someone exits, replaced, hiring/succession
- P4: Compensation, equity, valuation
- P5: Confidential strategy (exit, contracts, shareholders)
- P6: Authority conflict between executives

Default: ALLOW operational messages. Only block if message clearly communicates P1-P6.
"""
from __future__ import annotations

import json
import re

from agents import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    tool_input_guardrail,
)

# Denylist of phrase patterns that trigger the gate.
# Synchronized with .claude/hooks/discord-sensitivity-gate.py patterns.
# Keep minimal — over-broad patterns cause false positives that erode trust.
SENSITIVE_PATTERNS: dict[str, list[str]] = {
    "P1_personnel_judgment": [
        r"\b(no\s+est[aá]\s+rindiendo|bajo\s+rendimiento|no\s+est[aá]\s+dando\s+el\s+ancho)\b",
        r"\b(vamos\s+a\s+sacar|hay\s+que\s+sacar|tenemos\s+que\s+despedir)\b",
    ],
    "P3_succession": [
        r"\b(reemplaz(o|ar)|sucesi[oó]n|PIP|performance\s+improvement)\b",
        r"\b(est[aá]\s+saliendo|se\s+va\s+(en|el))\b",
    ],
    "P4_compensation": [
        r"\b(sueldo|salario|equity|stock\s+option|bonus)\s+de\s+\w+",
        r"\bvalorizaci[oó]n\s+TBC\b",
    ],
    "P5_confidential_strategy": [
        r"\b(exit|acqui[-\s]?hire|due\s+diligence|t[eé]rm\s+sheet)\b",
        r"\bacquirability\s+playbook\b",
    ],
    "P6_authority_conflict": [
        r"\b(conflicto\s+con|no\s+se\s+llevan\s+bien|autoridad\s+cuestionada)\b",
    ],
}

# Tool targets this guardrail applies to.
# In the vault, the same logic covers discord/slack/gmail — here we route
# via tool_name check inside the guardrail function.
EXTERNAL_COMMS_TOOLS: set[str] = {
    "slack_send_message",
    "slack_schedule_message",
    "send_gmail_message",
    "draft_gmail_message",
    "discord_reply",
    "discord_send_message",
}


def _scan_content(content: str) -> tuple[str, str] | None:
    """Return (category, matched_pattern) if content hits a sensitive pattern, else None."""
    if not content:
        return None
    content_lower = content.lower()
    for category, patterns in SENSITIVE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                return (category, pattern)
    return None


@tool_input_guardrail
def sensitivity_input_guardrail(
    data: ToolInputGuardrailData,
) -> ToolGuardrailFunctionOutput:
    """
    Scan external comms tool inputs for P1-P6 sensitivity patterns.

    Blocks the tool call if any pattern matches. Default ALLOW for everything
    else (operational messages, coordination, metrics) — per rule,
    sensitivity is about INFERENCE risk, not keyword paranoia.
    """
    tool_name = data.context.tool_name if data.context else ""

    # Scope: only apply to external comms tools
    if not any(t in tool_name for t in EXTERNAL_COMMS_TOOLS):
        return ToolGuardrailFunctionOutput(output_info="Out of scope (not external comms)")

    try:
        args = (
            json.loads(data.context.tool_arguments)
            if data.context and data.context.tool_arguments
            else {}
        )
    except json.JSONDecodeError:
        return ToolGuardrailFunctionOutput(
            output_info="Invalid JSON arguments — allowing but logging"
        )

    # Concatenate all string-like fields for scanning
    content_parts: list[str] = []
    for key in ("text", "body", "message", "content", "subject"):
        val = args.get(key)
        if isinstance(val, str):
            content_parts.append(val)
    content = " ".join(content_parts)

    hit = _scan_content(content)
    if hit is None:
        return ToolGuardrailFunctionOutput(
            output_info={"status": "passed", "tool": tool_name, "scanned_chars": len(content)}
        )

    category, pattern = hit
    return ToolGuardrailFunctionOutput.reject_content(
        message=(
            f"🛑 Sensitivity gate tripped on {tool_name} — detected {category}. "
            f"TBC confidentiality rule blocks external comms that could reveal "
            f"P1-P6 protected inferences. Review manually or rephrase."
        ),
        output_info={
            "category": category,
            "pattern_matched": pattern,
            "tool": tool_name,
            "action": "rejected",
        },
    )
