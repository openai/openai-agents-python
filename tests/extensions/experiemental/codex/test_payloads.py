from __future__ import annotations

import pytest

from agents.extensions.experimental.codex.items import (
    AgentMessageItem,
    TodoItem,
    TodoListItem,
    coerce_thread_item,
)


def test_dict_like_supports_mapping_access_for_dataclass_fields() -> None:
    item = AgentMessageItem(id="item-1", text="hello")

    assert item["id"] == "item-1"
    assert item["text"] == "hello"
    assert item["type"] == "agent_message"
    assert item.get("text") == "hello"
    assert item.get("missing", "fallback") == "fallback"
    assert "id" in item
    assert "missing" not in item
    assert object() not in item
    assert list(item.keys()) == ["id", "text", "type"]


def test_dict_like_raises_key_error_for_unknown_fields() -> None:
    item = AgentMessageItem(id="item-1", text="hello")

    with pytest.raises(KeyError, match="missing"):
        _ = item["missing"]


def test_dict_like_as_dict_recursively_converts_nested_dataclasses() -> None:
    item = TodoListItem(
        id="todo-list-1",
        items=[
            TodoItem(text="write tests", completed=True),
            TodoItem(text="run tests", completed=False),
        ],
    )

    assert item.as_dict() == {
        "id": "todo-list-1",
        "items": [
            {"text": "write tests", "completed": True},
            {"text": "run tests", "completed": False},
        ],
        "type": "todo_list",
    }


def test_coerce_thread_item_todo_list_rejects_non_mapping_entries() -> None:
    # Mirror the guard used by file_change/mcp_tool_call coercion so malformed
    # entries surface a clean TypeError instead of an AttributeError.
    with pytest.raises(TypeError, match="TodoItem must be a mapping"):
        coerce_thread_item({"type": "todo_list", "id": "todo-1", "items": [None]})


def test_coerce_thread_item_todo_list_passes_through_existing_items() -> None:
    existing = TodoItem(text="already", completed=True)
    result = coerce_thread_item({"type": "todo_list", "id": "todo-1", "items": [existing]})

    assert isinstance(result, TodoListItem)
    assert result.items == [existing]
