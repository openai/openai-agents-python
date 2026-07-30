"""Tests for #2020: strip stale Responses item ids + 404 rebuild retry."""

from __future__ import annotations

from typing import Any

import pytest

from agents.run_internal.items import (
    is_stale_response_item_not_found_error,
    run_items_to_input_items,
    strip_stale_response_item_ids,
)
from agents.run_internal.model_retry import get_response_with_retry


def test_strip_stale_response_item_ids_removes_reasoning_and_rs_ids():
    items = [
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "reasoning", "id": "rs_deadbeef123", "summary": [{"text": "think"}]},
        {"type": "function_call", "id": "fc_abc", "name": "f", "arguments": "{}"},
        {"type": "message", "role": "assistant", "content": "ok", "id": "msg_xyz"},
        {"type": "message", "role": "user", "content": "again", "id": "local-keep-me"},
    ]
    out = strip_stale_response_item_ids(items)
    assert out[0] == items[0]
    assert "id" not in out[1]
    assert out[1]["type"] == "reasoning"
    assert "id" not in out[2]
    assert out[2]["name"] == "f"
    assert "id" not in out[3]
    assert out[4]["id"] == "local-keep-me"


def test_is_stale_response_item_not_found_error_detects_rs_404():
    err = Exception(
        "Error code: 404 - {'error': {'message': \"Item with id "
        "'rs_3c4caff36e34c099006900f0e3c66481909c15ac262a15b060' not found.\"}}"
    )
    assert is_stale_response_item_not_found_error(err) is True
    assert is_stale_response_item_not_found_error(ValueError("unrelated")) is False


class _FakeRunItem:
    """Minimal RunItem-like object for conversion tests."""

    def __init__(self, item_type: str, raw: dict[str, Any]):
        self.type = item_type
        self.raw_item = raw

    def to_input_item(self) -> dict[str, Any]:
        return dict(self.raw_item)


def test_run_items_to_input_items_strips_under_default_policy():
    """Default policy (None) must strip server ids — not only omit (#2020)."""
    run_items = [
        _FakeRunItem(
            "reasoning_item",
            {"type": "reasoning", "id": "rs_default_path", "summary": [{"text": "t"}]},
        ),
        _FakeRunItem(
            "tool_call_item",
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "tool",
                "arguments": "{}",
            },
        ),
    ]
    # Default policy (preserve / None) — still strip server ids for multi-turn safety
    out = run_items_to_input_items(run_items, None)  # type: ignore[arg-type]
    assert len(out) == 2
    assert out[0]["type"] == "reasoning"
    assert "id" not in out[0]
    assert out[0]["summary"] == [{"text": "t"}]
    assert "id" not in out[1]
    assert out[1]["call_id"] == "call_1"


@pytest.mark.asyncio
async def test_get_response_with_retry_rebuilds_on_stale_item_404():
    """404 Item-with-id-not-found must sanitize input once and retry (live path)."""
    input_box: list[list[dict[str, Any]]] = [
        [
            {"type": "reasoning", "id": "rs_dead", "summary": []},
            {"type": "message", "role": "user", "content": "hi", "id": "msg_1"},
        ]
    ]
    attempts: list[list[Any]] = []

    class FakeUsage:
        def __init__(self) -> None:
            self.requests = 1

    class FakeResp:
        def __init__(self) -> None:
            self.usage = FakeUsage()

    async def get_response():
        ids = [it.get("id") for it in input_box[0]]
        attempts.append(list(ids))
        if any(isinstance(i, str) and i.startswith(("rs_", "fc_", "msg_")) for i in ids):
            raise Exception(
                "Error code: 404 - {'error': {'message': \"Item with id 'rs_dead' not found.\"}}"
            )
        return FakeResp()

    async def sanitize():
        input_box[0] = strip_stale_response_item_ids(input_box[0])

    async def rewind():
        return None

    resp = await get_response_with_retry(
        get_response=get_response,
        rewind=rewind,
        retry_settings=None,
        get_retry_advice=lambda req: None,
        previous_response_id=None,
        conversation_id=None,
        sanitize_stale_response_item_ids=sanitize,
    )
    assert isinstance(resp, FakeResp)
    assert len(attempts) == 2
    assert attempts[0] == ["rs_dead", "msg_1"]
    assert attempts[1] == [None, None]
    assert "id" not in input_box[0][0]
