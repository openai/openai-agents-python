"""Synthetic native b61 planning receipt data for durable checkpoint compatibility.

Captured by running test_native_planning_updates_checklist_and_preserves_goal_
without_auto_continuation against b61 and its matching native binary. Only
the two stored records were retained; credentials, paths and application
history are absent. Each test binds record IDs to its actual native history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def legacy_goal(thread_id: str, **values: Any) -> dict[str, Any]:
    receipt = json.loads(
        (Path(__file__).parent / "fixtures" / "native_planning_receipt.json").read_text()
    )
    return {**receipt["goal"], "thread_id": thread_id, **values}


def legacy_checklist(thread_id: str, turn_id: str) -> dict[str, Any]:
    receipt = json.loads(
        (Path(__file__).parent / "fixtures" / "native_planning_receipt.json").read_text()
    )
    return {**receipt["checklist"], "thread_id": thread_id, "turn_id": turn_id}
