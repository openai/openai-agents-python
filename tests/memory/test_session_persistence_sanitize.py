from __future__ import annotations

from typing import Any, cast

import pytest

from agents.items import TResponseInputItem
from agents.run_internal.session_persistence import _sanitize_openai_conversation_item


def _sanitize(item: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _sanitize_openai_conversation_item(cast(TResponseInputItem, item)))


@pytest.mark.parametrize(
    "item_type",
    [
        "file_search_call",
        "web_search_call",
        "computer_call",
        "code_interpreter_call",
        "image_generation_call",
        "local_shell_call",
        "local_shell_call_output",
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_call",
        "item_reference",
    ],
)
def test_sanitize_preserves_ids_required_by_openai_conversation_items(item_type: str) -> None:
    item = {"type": item_type, "id": f"{item_type}_abc123", "status": "completed"}

    sanitized = _sanitize(item)

    assert sanitized["id"] == f"{item_type}_abc123"
    assert sanitized["type"] == item_type


def test_sanitize_preserves_file_search_call_payload_id() -> None:
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


@pytest.mark.parametrize(
    "item",
    [
        {
            "type": "message",
            "id": "msg_abc",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        },
        {
            "type": "function_call",
            "id": "fc_abc",
            "call_id": "call_abc",
            "name": "get_weather",
            "arguments": "{}",
        },
        {"type": "function_call_output", "id": "out_abc", "call_id": "call_abc", "output": "{}"},
        {"type": "computer_call_output", "id": "ccout_abc", "call_id": "call_abc", "output": {}},
        {"type": "tool_search_call", "id": "ts_abc", "status": "completed"},
        {"type": "shell_call", "id": "sh_abc", "call_id": "call_abc", "action": {}},
    ],
)
def test_sanitize_strips_optional_or_policy_controlled_ids(item: dict[str, Any]) -> None:
    sanitized = _sanitize(item)

    assert "id" not in sanitized
    assert sanitized["type"] == item["type"]


def test_sanitize_preserves_reasoning_id_for_openai_conversations() -> None:
    item = {
        "type": "reasoning",
        "id": "rs_abc",
        "summary": [],
        "content": [],
        "provider_data": {"server": "metadata"},
    }

    sanitized = _sanitize(item)

    assert sanitized["id"] == "rs_abc"
    assert "provider_data" not in sanitized


def test_sanitize_preserves_reasoning_encrypted_content() -> None:
    item = {
        "type": "reasoning",
        "summary": [],
        "content": [],
        "encrypted_content": "encrypted",
    }

    sanitized = _sanitize(item)

    assert sanitized["encrypted_content"] == "encrypted"


def test_sanitize_always_strips_provider_data() -> None:
    item = {
        "type": "file_search_call",
        "id": "fs_keep",
        "status": "completed",
        "provider_data": {"model": "gpt-4.1-mini"},
    }

    sanitized = _sanitize(item)

    assert sanitized["id"] == "fs_keep"
    assert "provider_data" not in sanitized


def test_sanitize_passes_through_non_dict_items() -> None:
    class DummyItem:
        pass

    item = DummyItem()

    sanitized: Any = _sanitize_openai_conversation_item(cast(TResponseInputItem, item))

    assert sanitized is item
