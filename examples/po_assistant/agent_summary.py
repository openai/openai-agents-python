from __future__ import annotations

from typing import List

from agents import Agent, Runner

from .commit_models import PlanLineComputed, PlanResult


def build_summary_agent() -> Agent:
    return Agent(
        name="PO Commit Preview Summarizer",
        instructions=(
            "You summarize a PO commit preview for a human. Keep it concise and actionable. "
            "Show total requested, reserve-now vs backorder per line, and any missing data."
        ),
        model="gpt-5-2025-08-07",
    )


async def summarize_plan(result: PlanResult) -> str:
    agent = build_summary_agent()
    lines: List[str] = []
    for c in result.computed_lines:
        lines.append(
            f"- {c.product_option_id}: requested={c.requested_qty}, "
            f"available={c.available_qty}, reserve_now={c.reserve_now}, backorder={c.backorder_qty}"
        )
    text = (
        f"PO Preview (idempotency={result.idempotency_key})\n"
        f"Client link: {result.purchase_order.fields.get('Clients')}\n"
        + "\n".join(lines)
    )
    out = await Runner.run(agent, text)
    return str(out)


