"""
Financial Gate — blocks tool calls that modify financial state or send
emails with financial content without explicit confirmation.

Migrated from .claude/hooks/financial-gate.sh (vault).

Scope:
- Blocks writes to sheet ranges matching financial keywords (pl, ebitda, cogs, margen, royalty)
- Blocks gmail_message sends that contain financial numbers without `confirmed=true`

Philosophy: financial actions are Dragon, not Starship (rule `elon-frameworks.md`).
Fail-closed: if detection is ambiguous, block and ask for explicit confirmation.
"""
from __future__ import annotations

import json
import re

from agents import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    tool_input_guardrail,
)

# Tools whose argument space this guardrail scans
FINANCIAL_TARGET_TOOLS: set[str] = {
    "modify_sheet_values",
    "send_gmail_message",
    "draft_gmail_message",
}

# Regex patterns that indicate financial content
FINANCIAL_PATTERNS: list[re.Pattern[str]] = [
    # CLP amounts: $1.234.567 or $1,234,567 or 1234567 CLP
    re.compile(r"\$\s*\d{1,3}([.,]\d{3})+", re.IGNORECASE),
    re.compile(r"\b\d{4,}\s*CLP\b", re.IGNORECASE),
    # Financial terms (es/en) — allowlist of concepts that indicate financial state
    re.compile(
        r"\b(ebitda|cogs|royalty|regalias|margen|p&l|p ?y ?l|cash\s*flow|ebit|profit|loss)\b",
        re.IGNORECASE,
    ),
    # Percentage formulas common in financial analysis
    re.compile(r"\b(gross|net)\s+(margin|revenue|profit)\b", re.IGNORECASE),
]

# Sheet ranges that are financially sensitive
FINANCIAL_SHEET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"p[-_ ]?&[-_ ]?l", re.IGNORECASE),
    re.compile(r"presupuesto|budget", re.IGNORECASE),
    re.compile(r"cash[-_ ]?flow", re.IGNORECASE),
    re.compile(r"ebitda", re.IGNORECASE),
]


def _scan_financial_content(content: str) -> list[str]:
    """Return list of financial patterns matched in content."""
    hits: list[str] = []
    for pattern in FINANCIAL_PATTERNS:
        if pattern.search(content):
            hits.append(pattern.pattern)
    return hits


@tool_input_guardrail
def financial_input_guardrail(
    data: ToolInputGuardrailData,
) -> ToolGuardrailFunctionOutput:
    """
    Block financial-touching tool calls without explicit confirmation.

    Allows the call to proceed ONLY if:
    - Tool is out of scope (not sheet/gmail), OR
    - No financial content detected in args, OR
    - args contain `confirmed=true` or `financial_confirmed=true`
    """
    tool_name = data.context.tool_name if data.context else ""

    # Scope check
    if not any(t in tool_name for t in FINANCIAL_TARGET_TOOLS):
        return ToolGuardrailFunctionOutput(output_info="Out of scope (not financial tool)")

    try:
        args = (
            json.loads(data.context.tool_arguments)
            if data.context and data.context.tool_arguments
            else {}
        )
    except json.JSONDecodeError:
        return ToolGuardrailFunctionOutput.reject_content(
            message="🛑 Financial gate: invalid JSON arguments, fail-closed.",
            output_info={"reason": "invalid_json"},
        )

    # Explicit bypass flag (human-authorized)
    if args.get("confirmed") is True or args.get("financial_confirmed") is True:
        return ToolGuardrailFunctionOutput(
            output_info={"status": "bypassed", "reason": "explicit_confirmation"}
        )

    # Sheet range check (for modify_sheet_values)
    sheet_range = args.get("range", "") or args.get("sheet_name", "")
    if isinstance(sheet_range, str):
        for pattern in FINANCIAL_SHEET_PATTERNS:
            if pattern.search(sheet_range):
                return ToolGuardrailFunctionOutput.reject_content(
                    message=(
                        f"🛑 Financial gate: sheet range {sheet_range!r} matches "
                        f"financial pattern. Requires explicit confirmation. "
                        f"Set confirmed=true after human review."
                    ),
                    output_info={
                        "reason": "financial_sheet_range",
                        "range": sheet_range,
                        "pattern": pattern.pattern,
                    },
                )

    # Content scan (for emails, drafts, cell values)
    content_parts: list[str] = []
    for key in ("body", "text", "subject", "values", "message", "content"):
        val = args.get(key)
        if isinstance(val, str):
            content_parts.append(val)
        elif isinstance(val, list):
            content_parts.extend(str(v) for v in val)
    content = " ".join(content_parts)

    hits = _scan_financial_content(content)
    if hits:
        return ToolGuardrailFunctionOutput.reject_content(
            message=(
                f"🛑 Financial gate: {tool_name} contains financial content "
                f"({len(hits)} pattern match(es)). Requires explicit confirmation. "
                f"Set confirmed=true after human review."
            ),
            output_info={
                "reason": "financial_content_detected",
                "tool": tool_name,
                "patterns_matched": hits[:3],
            },
        )

    return ToolGuardrailFunctionOutput(
        output_info={"status": "passed", "tool": tool_name}
    )
