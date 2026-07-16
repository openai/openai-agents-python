"""Support escalation with approval, resumable state, sessions, and safe trace export.

This example composes SDK primitives into a support operations workflow:

1. A SQLite session retains the ticket conversation.
2. A mutation-like escalation tool pauses for human approval.
3. ``RunState`` is serialized before the decision and loaded again for resumption.
4. An additional trace processor writes support-safe JSONL metadata without payloads.

The publisher is deliberately simulated. Replace it only inside a trusted service boundary.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from agents import Agent, Runner, RunState, SQLiteSession, function_tool, trace
from agents.tracing import Span, Trace, TracingProcessor, add_trace_processor
from examples.auto_mode import confirm_with_fallback

CACHE_DIR = Path(".cache/customer_service/support_escalation")
STATE_PATH = CACHE_DIR / "run_state.json"
AUDIT_PATH = CACHE_DIR / "trace_events.jsonl"
SESSION_PATH = CACHE_DIR / "session.db"


class SupportTraceProcessor(TracingProcessor):
    """Write IDs, timing, and span types without customer or tool payloads."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _write(self, event: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")

    def on_trace_start(self, trace: Trace) -> None:
        self._write({"event": "trace_started", "trace_id": trace.trace_id})

    def on_trace_end(self, trace: Trace) -> None:
        self._write({"event": "trace_finished", "trace_id": trace.trace_id})

    def on_span_start(self, span: Span[Any]) -> None:
        self._write(
            {
                "event": "span_started",
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "span_type": span.span_data.type,
                "started_at": span.started_at,
            }
        )

    def on_span_end(self, span: Span[Any]) -> None:
        self._write(
            {
                "event": "span_finished",
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "span_type": span.span_data.type,
                "ended_at": span.ended_at,
                "has_error": span.error is not None,
            }
        )

    def force_flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


@function_tool
def inspect_ticket_evidence(ticket_id: str) -> str:
    """Return sanitized evidence for a synthetic support ticket."""
    return (
        f"Ticket {ticket_id}: webhook delivery failed three times with HTTP 503; "
        "the destination recovered on the next health check."
    )


@function_tool(needs_approval=True)
def publish_engineering_escalation(ticket_id: str, summary: str) -> str:
    """Simulate publishing an approved escalation package."""
    return f"Dry-run escalation created for {ticket_id}: {summary}"


agent = Agent(
    name="Support escalation agent",
    instructions=(
        "Investigate the ticket using the evidence tool. Then call the escalation tool with a "
        "concise, evidence-backed summary. Do not infer a root cause that the evidence does not show."
    ),
    tools=[inspect_ticket_evidence, publish_engineering_escalation],
)


async def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    add_trace_processor(SupportTraceProcessor(AUDIT_PATH))
    session = SQLiteSession("support-escalation-demo", SESSION_PATH)

    with trace("Support escalation", group_id="ticket_demo_001"):
        result = await Runner.run(
            agent,
            "Investigate synthetic ticket ticket_demo_001 and prepare an engineering escalation.",
            session=session,
        )

        if result.interruptions:
            state = result.to_state()
            STATE_PATH.write_text(json.dumps(state.to_json(), indent=2), encoding="utf-8")
            print(f"Paused run saved to {STATE_PATH}")

            stored_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            state = await RunState.from_json(agent, stored_state)

            for interruption in result.interruptions:
                print(f"Approval requested: {interruption.name} {interruption.arguments}")
                approved = confirm_with_fallback("Approve escalation? (y/n): ", default=False)
                if approved:
                    state.approve(interruption)
                else:
                    state.reject(
                        interruption,
                        rejection_message="Escalation publication was rejected by the reviewer.",
                    )

            result = await Runner.run(agent, state, session=session)

    print(result.final_output)
    print(f"Support-safe trace metadata written to {AUDIT_PATH}")
    session.close()


if __name__ == "__main__":
    asyncio.run(main())
