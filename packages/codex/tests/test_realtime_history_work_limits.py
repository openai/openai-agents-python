"""Canonical transcript snapshots share a lifetime copy-work ceiling."""

from __future__ import annotations

import pytest

from agents.realtime import RealtimeGuardrailResult, RealtimeHistory
from tests.test_realtime_guardrail_retention import owner_for


def history_event(item: str, text: str = "", *, completed: bool = False) -> dict:
    return {
        "HistoryItemCompleted" if completed else "HistoryItemStarted": {
            "id": item,
            "realtime_session_id": "session",
            "type": "message",
            "role": "assistant",
            "text": text,
        }
    }


def delta_event(item: str, text: str) -> dict:
    return {"HistoryTranscriptDelta": {"item_id": item, "delta": text}}


async def test_tiny_history_deltas_exhaust_work_with_event_queue_drained() -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False))
    owner._payload(history_event("fragmented"), None)
    owner._dequeue()
    with pytest.raises(RuntimeError, match="history text work capacity"):
        for _ in range(24_000):
            owner._payload(delta_event("fragmented", "x"), None)
            owner._dequeue()
    assert owner.history[0].text.startswith("x")
    assert len(owner.history[0].text) < 24_000


async def test_history_work_survives_completed_items_and_replayed_replacements(monkeypatch) -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False))
    monkeypatch.setattr(owner, "_HISTORY_TEXT_WORK_LIMIT", 48, raising=False)
    owner._payload(history_event("first", "a" * 8), None)
    owner._payload(delta_event("first", "b" * 8), None)
    # Completion replaces the snapshot, but does not undo work already spent.
    for _ in range(2):
        owner._payload(history_event("first", "done", completed=True), None)
    owner._payload(history_event("second", "c" * 8), None)
    owner._payload(delta_event("second", "d" * 8), None)
    owner._payload(history_event("unrelated", "done", completed=True), None)
    with pytest.raises(RuntimeError, match="history text work capacity"):
        owner._payload(delta_event("second", "e" * 8), None)
    assert owner.history[1].text == "c" * 8 + "d" * 8


async def test_history_capacity_rejection_leaves_current_snapshot_and_queue_unchanged(
    monkeypatch,
) -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False))
    owner._payload(history_event("bounded", "accepted"), None)
    original = owner.history[0]
    event = owner._dequeue()
    assert isinstance(event, RealtimeHistory)
    monkeypatch.setattr(owner, "_BUFFER_BYTES_LIMIT", owner._history_bytes + 4)
    with pytest.raises(RuntimeError, match="history byte capacity"):
        owner._payload(delta_event("bounded", "xx"), None)
    assert owner.history == (original,)
    assert owner._queue.empty()
    assert event.item.text == "accepted"


async def test_normal_history_snapshots_remain_immutable_and_empty_deltas_spend_no_work(
    monkeypatch,
) -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False))
    monkeypatch.setattr(owner, "_HISTORY_TEXT_WORK_LIMIT", 16, raising=False)
    owner._payload(history_event("normal", "a" * 8), None)
    first = owner._dequeue()
    assert isinstance(first, RealtimeHistory)
    owner._payload(delta_event("normal", "b" * 8), None)
    second = owner._dequeue()
    assert isinstance(second, RealtimeHistory)
    for _ in range(100):
        owner._payload(delta_event("normal", ""), None)
        owner._dequeue()
    owner._payload(history_event("other", "final", completed=True), None)
    assert first.item.text == "a" * 8
    assert second.item.text == "a" * 8 + "b" * 8
    assert [item.text for item in owner.history] == ["a" * 8 + "b" * 8, "final"]
