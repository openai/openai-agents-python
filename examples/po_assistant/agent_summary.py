from __future__ import annotations

from typing import Any, List

from agents import Agent, GuardrailFunctionOutput, OutputGuardrail, RunConfig, Runner

from .commit_models import PlanResult
from .trace_logger import TraceLogger


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
        f"Client link: {result.purchase_order.fields.get('Clients')}\n" + "\n".join(lines)
    )
    out = await Runner.run(agent, text)
    return str(out)


def _risk_guardrail_function(_, __, agent_output: Any) -> GuardrailFunctionOutput:
    """Simple, structured risk checks for the summary text.

    - Flags if summary is empty or too short.
    - Flags if obviously unsafe terms appear (e.g., leverage suggestions) as a placeholder.
    Returns structured violations in `output_info`.
    """
    violations: list[str] = []
    text = str(agent_output or "").strip()
    if len(text) < 8:
        violations.append("summary_too_short")
    lower = text.lower()
    if any(
        bad in lower for bad in ("guaranteed return", "all-in", "100% of capital", "x100 leverage")
    ):
        violations.append("unsafe_language")

    return GuardrailFunctionOutput(
        output_info={"violations": violations},
        tripwire_triggered=len(violations) > 0,
    )


SUMMARY_OUTPUT_GUARDRAIL = OutputGuardrail(
    guardrail_function=_risk_guardrail_function, name="summary_risk"
)


async def summarize_plan_with_guardrail(result: PlanResult) -> dict[str, Any]:
    """Run the summarizer with an output guardrail and return structured result.

    On pass: {"ok": true, "summary": str}
    On fail: {"ok": false, "violations": [...]} (no exception raised here)
    """
    agent = build_summary_agent()
    lines: List[str] = []
    for c in result.computed_lines:
        lines.append(
            f"- {c.product_option_id}: requested={c.requested_qty}, "
            f"available={c.available_qty}, reserve_now={c.reserve_now}, backorder={c.backorder_qty}"
        )
    text = (
        f"PO Preview (idempotency={result.idempotency_key})\n"
        f"Client link: {result.purchase_order.fields.get('Clients')}\n" + "\n".join(lines)
    )

    try:
        result_run = await Runner.run(
            agent,
            text,
            run_config=RunConfig(output_guardrails=[SUMMARY_OUTPUT_GUARDRAIL]),
        )
        summary_text = str(result_run.final_output or result_run)
        # Ensure guardrail enforcement even if SDK guardrail didn't trigger (e.g., mocked Runner).
        gr = _risk_guardrail_function(None, None, summary_text)
        if gr.tripwire_triggered:
            info = gr.output_info if isinstance(gr.output_info, dict) else {}
            return {"ok": False, "violations": info.get("violations", ["guardrail_failed"])}
        return {"ok": True, "summary": summary_text}
    except Exception as ex:  # Guardrail tripwire or other errors
        # Extract violations if available.
        violations: list[str] = []
        try:
            # Agents exceptions provide run_data only on some error paths; keep best-effort.
            from agents import OutputGuardrailTripwireTriggered  # local import to avoid cycles

            if isinstance(ex, OutputGuardrailTripwireTriggered):
                info = ex.guardrail_result.output.output_info
                if isinstance(info, dict):
                    violations = [str(v) for v in info.get("violations", [])]
        except Exception:
            pass
        return {"ok": False, "violations": violations or ["guardrail_failed"]}


async def stream_summary_events(result: PlanResult, task_id: str):
    """Yield NDJSON-friendly dict events for UI while logging JSONL traces on disk.

    Events include agent updates, raw response deltas, and run item events (tool calls, messages).
    """
    agent = build_summary_agent()
    lines: List[str] = []
    for c in result.computed_lines:
        lines.append(
            f"- {c.product_option_id}: requested={c.requested_qty}, "
            f"available={c.available_qty}, reserve_now={c.reserve_now}, backorder={c.backorder_qty}"
        )
    text = (
        f"PO Preview (idempotency={result.idempotency_key})\n"
        f"Client link: {result.purchase_order.fields.get('Clients')}\n" + "\n".join(lines)
    )

    tracer = TraceLogger(task_id)
    result_stream = Runner.run_streamed(agent, input=text)
    async for event in result_stream.stream_events():
        evt_type = getattr(event, "type", "unknown")
        payload: Any = None
        if evt_type == "agent_updated_stream_event":
            new_agent = getattr(event, "new_agent", None)
            agent_name = getattr(new_agent, "name", "") if new_agent else ""
            payload = {"agent": agent_name}
        elif evt_type == "raw_response_event":
            data = getattr(event, "data", None)
            # Only forward text deltas and reasoning deltas; avoid echoing sensitive input.
            if data and getattr(data, "type", "").endswith("output_text.delta"):
                payload = {"delta": getattr(data, "delta", "")}
            elif data and getattr(data, "type", "").endswith("reasoning_summary_text.delta"):
                payload = {"reasoning_delta": getattr(data, "delta", "")}
            else:
                payload = {"raw_type": getattr(data, "type", None)}
        elif evt_type == "run_item_stream_event":
            name = getattr(event, "name", "")
            item = getattr(event, "item", None)
            payload = {"name": name, "item_type": getattr(item, "type", None)}
        else:
            payload = {}

        tracer.log(evt_type, payload)
        yield {"type": evt_type, "payload": payload}
