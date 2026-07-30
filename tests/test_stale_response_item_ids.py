"""Tests for #2020: strip stale Responses item ids + 404 rebuild retry."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents.items import TResponseInputItem
from agents.run_internal.items import (
    is_stale_response_item_not_found_error,
    run_items_to_input_items,
    strip_stale_response_item_ids,
)
from agents.run_internal.model_retry import get_response_with_retry
from agents.usage import Usage


def test_strip_stale_response_item_ids_removes_reasoning_and_rs_ids() -> None:
    items: list[TResponseInputItem] = [
        cast(
            TResponseInputItem,
            {"type": "message", "role": "user", "content": "hi"},
        ),
        cast(
            TResponseInputItem,
            {
                "type": "reasoning",
                "id": "rs_deadbeef123",
                "summary": [{"text": "think"}],
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "function_call",
                "id": "fc_abc",
                "name": "f",
                "arguments": "{}",
                "call_id": "call_abc",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "message",
                "role": "assistant",
                "content": "ok",
                "id": "msg_xyz",
            },
        ),
        cast(
            TResponseInputItem,
            {
                "type": "message",
                "role": "user",
                "content": "again",
                "id": "local-keep-me",
            },
        ),
    ]
    out = strip_stale_response_item_ids(items)
    assert out[0] == items[0]
    assert isinstance(out[1], dict)
    assert "id" not in out[1]
    assert out[1].get("type") == "reasoning"
    assert isinstance(out[2], dict)
    assert "id" not in out[2]
    assert out[2].get("name") == "f"
    assert isinstance(out[3], dict)
    assert "id" not in out[3]
    assert isinstance(out[4], dict)
    assert out[4].get("id") == "local-keep-me"


def test_is_stale_response_item_not_found_error_detects_rs_404() -> None:
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

    def to_input_item(self) -> TResponseInputItem:
        return cast(TResponseInputItem, dict(self.raw_item))


def test_run_items_to_input_items_strips_under_default_policy() -> None:
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
    assert isinstance(out[0], dict)
    assert out[0].get("type") == "reasoning"
    assert "id" not in out[0]
    assert out[0].get("summary") == [{"text": "t"}]
    assert isinstance(out[1], dict)
    assert "id" not in out[1]
    assert out[1].get("call_id") == "call_1"


@pytest.mark.asyncio
async def test_get_response_with_retry_rebuilds_on_stale_item_404() -> None:
    """404 Item-with-id-not-found must sanitize input once and retry (live path)."""
    input_box: list[list[TResponseInputItem]] = [
        [
            cast(
                TResponseInputItem,
                {"type": "reasoning", "id": "rs_dead", "summary": []},
            ),
            cast(
                TResponseInputItem,
                {"type": "message", "role": "user", "content": "hi", "id": "msg_1"},
            ),
        ]
    ]
    attempts: list[list[Any]] = []

    class FakeResp:
        def __init__(self) -> None:
            # Real Usage so apply_retry_attempt_usage can attach failed-attempt accounting.
            self.usage = Usage()

    async def get_response() -> FakeResp:
        ids = [item.get("id") if isinstance(item, dict) else None for item in input_box[0]]
        attempts.append(list(ids))
        if any(isinstance(i, str) and i.startswith(("rs_", "fc_", "msg_")) for i in ids):
            raise Exception(
                "Error code: 404 - {'error': {'message': \"Item with id 'rs_dead' not found.\"}}"
            )
        return FakeResp()

    async def sanitize() -> None:
        input_box[0] = strip_stale_response_item_ids(input_box[0])

    async def rewind() -> None:
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
    assert isinstance(input_box[0][0], dict)
    assert "id" not in input_box[0][0]
