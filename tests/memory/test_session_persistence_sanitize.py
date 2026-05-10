"""Regression tests for `_sanitize_openai_conversation_item` (issue #3267).

`OpenAIConversationsSession` persists items via `conversations.items.create()`. The
OpenAI Responses API param schema marks `id` as `Required[str]` for hosted-tool call
item types (e.g. `file_search_call`, `web_search_call`, `mcp_call`, ...). The sanitizer
must therefore preserve `id` for those types while still stripping it for items where
`id` is optional (e.g. `function_call`, plain `message`).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents.items import TResponseInputItem
from agents.run_internal.session_persistence import (
    _HOSTED_TOOL_ITEM_TYPES_REQUIRING_ID,
    _sanitize_openai_conversation_item,
)


def _sanitize(item: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _sanitize_openai_conversation_item(cast(TResponseInputItem, item)))


@pytest.mark.parametrize("item_type", sorted(_HOSTED_TOOL_ITEM_TYPES_REQUIRING_ID))
def test_hosted_tool_call_item_preserves_id(item_type: str) -> None:
    item = {"type": item_type, "id": f"{item_type}_abc123", "status": "completed"}

    sanitized = _sanitize(item)

    assert sanitized["id"] == f"{item_type}_abc123", (
        f"sanitizer must preserve `id` for {item_type!r} or "
        f"conversations.items.create() rejects with HTTP 400 (issue #3267)"
    )
    assert sanitized["type"] == item_type


def test_file_search_call_full_payload_preserves_id() -> None:
    item = {
        "type": "file_search_call",
        "id": "fs_call_abc",
        "queries": ["latest q3 revenue"],
        "status": "completed",
        "results": [{"file_id": "file_1", "filename": "q3.pdf", "score": 0.9, "text": "..."}],
    }

    sanitized = _sanitize(item)

    assert sanitized["id"] == "fs_call_abc"
    assert sanitized["queries"] == ["latest q3 revenue"]
    assert sanitized["status"] == "completed"


def test_function_call_item_still_strips_id() -> None:
    # `id` is optional for function_call in the OpenAI param schema, and the existing
    # behaviour relies on stripping it before persistence to avoid stale-id replay.
    item = {
        "type": "function_call",
        "id": "fc_abc",
        "call_id": "call_abc",
        "name": "get_weather",
        "arguments": "{}",
    }

    sanitized = _sanitize(item)

    assert "id" not in sanitized
    assert sanitized["call_id"] == "call_abc"
    assert sanitized["type"] == "function_call"


def test_plain_message_still_strips_id() -> None:
    item = {
        "type": "message",
        "id": "msg_abc",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
    }

    sanitized = _sanitize(item)

    assert "id" not in sanitized
    assert sanitized["role"] == "assistant"


def test_provider_data_always_stripped() -> None:
    item = {
        "type": "file_search_call",
        "id": "fs_keep",
        "status": "completed",
        "provider_data": {"model": "gpt-4o"},
    }

    sanitized = _sanitize(item)

    assert "provider_data" not in sanitized
    assert sanitized["id"] == "fs_keep"


def test_non_dict_item_passthrough() -> None:
    # The sanitizer is permissive: pydantic models / non-dicts pass through untouched.
    class _Dummy:
        pass

    obj = _Dummy()
    result: Any = _sanitize_openai_conversation_item(cast(TResponseInputItem, obj))
    assert result is obj
