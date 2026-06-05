"""Tests for stripping stale reasoning item IDs from local session history."""
from __future__ import annotations

from agents.run_internal.items import strip_stale_reasoning_item_ids


class TestStripStaleReasoningItemIds:
    def test_strips_id_from_reasoning_item(self) -> None:
        items: list[dict[str, object]] = [
            {"type": "reasoning", "id": "rs_deadbeef", "summary": []},
        ]
        result = strip_stale_reasoning_item_ids(items)  # type: ignore[arg-type]
        assert result[0].get("id") is None  # type: ignore[union-attr]

    def test_preserves_non_reasoning_item_ids(self) -> None:
        items: list[dict[str, object]] = [
            {"type": "message", "id": "msg_123", "role": "user", "content": "hi"},
            {"type": "function_call", "id": "fc_456", "call_id": "c1", "name": "f", "arguments": "{}"},
        ]
        result = strip_stale_reasoning_item_ids(items)  # type: ignore[arg-type]
        assert result[0].get("id") == "msg_123"  # type: ignore[union-attr]
        assert result[1].get("id") == "fc_456"  # type: ignore[union-attr]

    def test_reasoning_without_id_passes_through(self) -> None:
        items: list[dict[str, object]] = [
            {"type": "reasoning", "summary": []},
        ]
        result = strip_stale_reasoning_item_ids(items)  # type: ignore[arg-type]
        assert "id" not in result[0]  # type: ignore[arg-type]

    def test_mixed_items_strip_only_reasoning(self) -> None:
        items: list[dict[str, object]] = [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "message", "id": "msg_1", "role": "assistant", "content": "ok"},
            {"type": "reasoning", "id": "rs_2", "summary": []},
            {"type": "function_call_output", "call_id": "c1", "output": "result"},
        ]
        result = strip_stale_reasoning_item_ids(items)  # type: ignore[arg-type]
        assert result[0].get("id") is None  # type: ignore[union-attr]
        assert result[1].get("id") == "msg_1"  # type: ignore[union-attr]
        assert result[2].get("id") is None  # type: ignore[union-attr]
        assert result[3].get("call_id") == "c1"  # type: ignore[union-attr]

    def test_empty_list(self) -> None:
        result = strip_stale_reasoning_item_ids([])
        assert result == []
