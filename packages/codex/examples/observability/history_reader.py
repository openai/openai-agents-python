"""Read a settled Session's history projection, not a complete audit log."""

from __future__ import annotations

from typing import Any

from agents import Session


async def read_history(session: Session[Any, Any]) -> dict[str, Any]:
    """Keep unavailable fields null and retain unpaired outputs explicitly."""
    turns = [turn async for turn in session.turns()]
    items = [item async for item in session.items()]
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    results = []
    for item in items:
        if item["type"] == "function_call":
            calls[(item["turn_id"], item["call_id"])] = dict(item)
        elif item["type"] == "function_call_output":
            call = calls.pop((item["turn_id"], item["call_id"]), None)
            results.append(
                {
                    "turn_id": item["turn_id"],
                    "call_id": item["call_id"],
                    "name": call["name"] if call else None,
                    "arguments": call["arguments"] if call else None,
                    "status": item["status"],
                    "output": item["output"],
                    "error": item["error"],
                }
            )
    return {
        "turns": turns,
        "item_types": [item["type"] for item in items],
        "function_results": results,
    }
