from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from agents import ApplyPatchOperation, ApplyPatchResult, apply_patch_tool
from agents.session import _call_callback


class Editor:
    def __init__(self) -> None:
        self.operations: list[ApplyPatchOperation] = []
        self.thread: int | None = None
        self.result: ApplyPatchResult | str | None = None

    def create_file(self, operation: ApplyPatchOperation) -> Any:
        self.thread = threading.get_ident()
        self.operations.append(operation)
        return self.result

    update_file = create_file
    delete_file = create_file


async def invoke(editor: Editor, operation: dict[str, Any]) -> Any:
    entry = apply_patch_tool(editor)._capture(object())
    parsed = entry._parse_arguments(
        {"type": "apply_patch_call", "call_id": "patch-1", "operation": operation}
    )
    value = await _call_callback(entry.callback, lambda: True, object(), **entry._arguments(parsed))
    return entry.output_adapter.validate_python(value)


@pytest.mark.asyncio
async def test_custom_paths_and_move_destination_remain_application_owned() -> None:
    editor = Editor()
    output = await invoke(
        editor,
        {
            "type": "update_file",
            "path": "tenant://one/../opaque",
            "diff": "custom-format",
            "move_to": "remote://two",
        },
    )
    assert editor.operations[0].path == "tenant://one/../opaque"
    assert editor.operations[0].move_to == "remote://two"
    assert editor.operations[0].diff == "custom-format"
    assert editor.operations[0].ctx_wrapper is not None
    assert output.success
    assert editor.thread != threading.get_ident()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,status,text",
    [
        (None, "completed", None),
        ("done", "completed", "done"),
        (ApplyPatchResult("failed", "refused"), "failed", "refused"),
    ],
)
async def test_editor_result_retains_explicit_status_and_text(
    result: Any, status: str, text: str | None
) -> None:
    editor = Editor()
    editor.result = result
    output = await invoke(editor, {"type": "delete_file", "path": "custom path"})
    assert output.success is (status == "completed")
    assert json.loads(output.content_items[0].text) == {"status": status, "output": text}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["", False, 0])
async def test_editor_rejects_invalid_falsy_status(status: Any) -> None:
    editor = Editor()
    editor.result = ApplyPatchResult(status=status)
    with pytest.raises(TypeError, match="Invalid Apply Patch result"):
        await invoke(editor, {"type": "create_file", "path": "custom", "diff": "diff"})


@pytest.mark.asyncio
async def test_async_editor_uses_existing_awaitable_callback_path() -> None:
    editor = Editor()
    loop_thread = threading.get_ident()

    async def execute(operation: ApplyPatchOperation) -> ApplyPatchResult:
        assert threading.get_ident() == loop_thread
        await asyncio.sleep(0)
        editor.operations.append(operation)
        return ApplyPatchResult(output="async")

    editor.create_file = execute  # type: ignore[method-assign]
    output = await invoke(editor, {"type": "create_file", "path": "custom", "diff": "diff"})
    assert len(editor.operations) == 1
    assert output.success


@pytest.mark.parametrize(
    "operation",
    [
        {"type": "create_file", "path": "x", "diff": 17},
        {"type": "update_file", "path": "x", "diff": "d", "move_to": ""},
        {"type": "delete_file", "path": None},
    ],
)
def test_invalid_operation_fails_before_editor_entry(operation: Any) -> None:
    editor = Editor()
    entry = apply_patch_tool(editor)._capture(None)
    with pytest.raises(ValidationError):
        entry._parse_arguments(
            {"type": "apply_patch_call", "call_id": "patch-1", "operation": operation}
        )
    assert editor.operations == []


def test_captured_method_and_native_declaration_identity() -> None:
    editor = Editor()
    tool = apply_patch_tool(editor)
    entry = tool._capture(None)
    assert json.loads(entry.spec_json) == {"type": "applyPatch"}
    original = entry.callback.callbacks["create_file"]
    editor.create_file = lambda operation: "replacement"  # type: ignore[method-assign]
    assert entry.callback.callbacks["create_file"] == original
    assert tool.ref.name == "apply_patch"
    for changes in (
        {"name": "other"},
        {"namespace": "other"},
        {"defer_loading": True},
        {"finish_turn": True},
    ):
        with pytest.raises(TypeError):
            replace(tool, **changes)


@pytest.mark.parametrize("owned", [False, True])
def test_only_durable_owner_projects_unknown_application_edit(owned: bool) -> None:
    from agents._history import project_history

    raw = {
        "type": "apply_patch_call",
        "id": "apc_item",
        "call_id": "patch-1",
        "operation": {"type": "delete_file", "path": "custom"},
    }
    record = {"type": "response_item", "payload": raw}
    if owned:
        record["metadata"] = {"sdk_application_tool": "apply_patch"}
    history = [{"type": "turn_context", "payload": {"turn_id": "turn"}}, record]
    uncertain = []
    _, items = project_history(history, "thread", uncertain_calls=uncertain)
    assert len(uncertain) == int(owned)
    if owned:
        assert uncertain[0].name == "apply_patch"
        assert uncertain[0].arguments == raw
        assert items[0]["status"] == "in_progress"
    else:
        assert items == []
