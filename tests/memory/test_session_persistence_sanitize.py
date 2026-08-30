from __future__ import annotations

from typing import Any, cast

import pytest

from agents.items import TResponseInputItem
from agents.run_internal.session_persistence import (
    _canonicalize_openai_conversation_item_for_reconciliation,
    _sanitize_openai_conversation_item,
)


def _sanitize(item: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _sanitize_openai_conversation_item(cast(TResponseInputItem, item)))


def _canonicalize(item: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _canonicalize_openai_conversation_item_for_reconciliation(cast(TResponseInputItem, item)),
    )


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
        "program",
        "program_output",
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


def test_sanitize_preserves_program_payload_id() -> None:
    item = {
        "type": "program",
        "id": "program_abc",
        "call_id": "call_program",
        "code": 'lookup_inventory(sku="A-1")',
        "fingerprint": "fingerprint",
    }

    sanitized = _sanitize(item)

    assert sanitized["id"] == "program_abc"
    assert sanitized["call_id"] == "call_program"
    assert sanitized["code"] == 'lookup_inventory(sku="A-1")'
    assert sanitized["fingerprint"] == "fingerprint"


def test_sanitize_preserves_program_output_payload_id() -> None:
    item = {
        "type": "program_output",
        "id": "program_output_abc",
        "call_id": "call_program",
        "result": '{"available_units":42}',
        "status": "completed",
    }

    sanitized = _sanitize(item)

    assert sanitized["id"] == "program_output_abc"
    assert sanitized["call_id"] == "call_program"
    assert sanitized["result"] == '{"available_units":42}'
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
        {
            "type": "function_call",
            "id": "fc_prog",
            "call_id": "call_abc",
            "name": "get_weather",
            "arguments": "{}",
            "caller": {"type": "program", "caller_id": "call_program"},
        },
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


@pytest.mark.parametrize(
    ("shorthand", "normalized"),
    [
        (
            {"role": "user", "content": "hello"},
            {
                "id": "msg_user",
                "type": "message",
                "role": "user",
                "status": "completed",
                "phase": None,
                "content": [
                    {
                        "type": "input_text",
                        "text": "hello",
                        "prompt_cache_breakpoint": None,
                    }
                ],
            },
        ),
        (
            {"role": "assistant", "content": "hello"},
            {
                "id": "msg_assistant",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "phase": None,
                "content": [
                    {
                        "type": "output_text",
                        "text": "hello",
                        "annotations": [],
                        "logprobs": None,
                    }
                ],
            },
        ),
        (
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "lookup",
                "arguments": "{}",
            },
            {
                "id": "fc_123",
                "type": "function_call",
                "call_id": "call_123",
                "name": "lookup",
                "arguments": "{}",
                "status": "completed",
                "created_by": "server",
            },
        ),
    ],
)
def test_conversation_reconciliation_canonicalizes_message_response_defaults(
    shorthand: dict[str, Any], normalized: dict[str, Any]
) -> None:
    assert _canonicalize(shorthand) == _canonicalize(normalized)


@pytest.mark.parametrize(
    "different",
    [
        {"role": "user", "content": "different"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "hello", "status": "incomplete"},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "hello"},
                {"type": "input_text", "text": "again"},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "hello",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        },
    ],
)
def test_conversation_reconciliation_preserves_semantic_message_differences(
    different: dict[str, Any],
) -> None:
    baseline = {"role": "user", "content": "hello"}

    assert _canonicalize(different) != _canonicalize(baseline)


def test_conversation_reconciliation_preserves_nonempty_assistant_metadata() -> None:
    shorthand = {"role": "assistant", "content": "hello"}
    annotated = {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": "hello",
                "annotations": [
                    {
                        "type": "url_citation",
                        "url": "https://example.com",
                        "title": "Example",
                        "start_index": 0,
                        "end_index": 5,
                    }
                ],
            }
        ],
    }

    assert _canonicalize(annotated) != _canonicalize(shorthand)


def test_conversation_reconciliation_preserves_nonterminal_function_call_status() -> None:
    submitted = {
        "type": "function_call",
        "call_id": "call_123",
        "name": "lookup",
        "arguments": "{}",
    }
    incomplete = {**submitted, "status": "incomplete"}

    assert _canonicalize(incomplete) != _canonicalize(submitted)


def test_conversation_reconciliation_strips_shell_output_actor_metadata() -> None:
    output_chunk = {
        "stdout": "done",
        "stderr": "",
        "outcome": {"type": "exit", "exit_code": 0},
    }
    submitted = {
        "type": "shell_call_output",
        "call_id": "call_shell",
        "status": "completed",
        "output": [output_chunk],
    }
    returned = {
        **submitted,
        "id": "shell_output_123",
        "created_by": "server",
        "output": [{**output_chunk, "created_by": "server"}],
    }

    assert _canonicalize(returned) == _canonicalize(submitted)
