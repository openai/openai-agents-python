from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from agents import Agent, ApplyPatchTool, RunConfig, RunContextWrapper, RunHooks
from agents._run_impl import ApplyPatchAction, ToolRunApplyPatchCall
from agents.editor import ApplyPatchOperation, ApplyPatchResult
from agents.items import ToolApprovalItem, ToolCallOutputItem


@dataclass
class DummyApplyPatchCall:
    type: str
    call_id: str
    operation: dict[str, Any]


class RecordingEditor:
    def __init__(self) -> None:
        self.operations: list[ApplyPatchOperation] = []

    def create_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        self.operations.append(operation)
        return ApplyPatchResult(output=f"Created {operation.path}")

    def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        self.operations.append(operation)
        return ApplyPatchResult(status="completed", output=f"Updated {operation.path}")

    def delete_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
        self.operations.append(operation)
        return ApplyPatchResult(output=f"Deleted {operation.path}")


@pytest.mark.asyncio
async def test_apply_patch_tool_success() -> None:
    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor)
    tool_call = DummyApplyPatchCall(
        type="apply_patch_call",
        call_id="call_apply",
        operation={"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"},
    )
    tool_run = ToolRunApplyPatchCall(tool_call=tool_call, apply_patch_tool=tool)
    agent = Agent(name="patcher", tools=[tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert "Updated tasks.md" in result.output
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["type"] == "apply_patch_call_output"
    assert raw_item["status"] == "completed"
    assert raw_item["call_id"] == "call_apply"
    assert editor.operations[0].type == "update_file"
    assert editor.operations[0].ctx_wrapper is context_wrapper
    assert isinstance(raw_item["output"], str)
    assert raw_item["output"].startswith("Updated tasks.md")
    input_payload = result.to_input_item()
    assert isinstance(input_payload, dict)
    payload_dict = cast(dict[str, Any], input_payload)
    assert payload_dict["type"] == "apply_patch_call_output"
    assert payload_dict["status"] == "completed"


@pytest.mark.asyncio
async def test_apply_patch_tool_failure() -> None:
    class ExplodingEditor(RecordingEditor):
        def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
            raise RuntimeError("boom")

    tool = ApplyPatchTool(editor=ExplodingEditor())
    tool_call = DummyApplyPatchCall(
        type="apply_patch_call",
        call_id="call_apply_fail",
        operation={"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"},
    )
    tool_run = ToolRunApplyPatchCall(tool_call=tool_call, apply_patch_tool=tool)
    agent = Agent(name="patcher", tools=[tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert "boom" in result.output
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["status"] == "failed"
    assert isinstance(raw_item.get("output"), str)
    input_payload = result.to_input_item()
    assert isinstance(input_payload, dict)
    payload_dict = cast(dict[str, Any], input_payload)
    assert payload_dict["type"] == "apply_patch_call_output"
    assert payload_dict["status"] == "failed"


@pytest.mark.asyncio
async def test_apply_patch_tool_accepts_mapping_call() -> None:
    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor)
    tool_call: dict[str, Any] = {
        "type": "apply_patch_call",
        "call_id": "call_mapping",
        "operation": {
            "type": "create_file",
            "path": "notes.md",
            "diff": "+hello\n",
        },
    }
    tool_run = ToolRunApplyPatchCall(tool_call=tool_call, apply_patch_tool=tool)
    agent = Agent(name="patcher", tools=[tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["call_id"] == "call_mapping"
    assert editor.operations[0].path == "notes.md"
    assert editor.operations[0].ctx_wrapper is context_wrapper


@pytest.mark.asyncio
async def test_apply_patch_tool_needs_approval_returns_approval_item() -> None:
    """Test that apply_patch tool with needs_approval=True returns ToolApprovalItem."""

    async def needs_approval(_ctx, _operation, _call_id) -> bool:
        return True

    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval)
    tool_call = DummyApplyPatchCall(
        type="apply_patch_call",
        call_id="call_apply",
        operation={"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"},
    )
    tool_run = ToolRunApplyPatchCall(tool_call=tool_call, apply_patch_tool=tool)
    agent = Agent(name="patcher", tools=[tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    from agents.items import ToolApprovalItem

    assert isinstance(result, ToolApprovalItem)
    assert result.tool_name == "apply_patch"
    assert result.name == "apply_patch"


@pytest.mark.asyncio
async def test_apply_patch_tool_needs_approval_rejected_returns_rejection() -> None:
    """Test that apply_patch tool with needs_approval that is rejected returns rejection output."""

    async def needs_approval(_ctx, _operation, _call_id) -> bool:
        return True

    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval)
    tool_call = DummyApplyPatchCall(
        type="apply_patch_call",
        call_id="call_apply",
        operation={"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"},
    )
    tool_run = ToolRunApplyPatchCall(tool_call=tool_call, apply_patch_tool=tool)
    agent = Agent(name="patcher", tools=[tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    # Pre-reject the tool call
    approval_item = ToolApprovalItem(
        agent=agent, raw_item=cast(dict[str, Any], tool_call), tool_name="apply_patch"
    )
    context_wrapper.reject_tool(approval_item)

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert "Tool execution was not approved" in result.output
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["type"] == "apply_patch_call_output"
    assert raw_item["status"] == "failed"
    assert raw_item["output"] == "Tool execution was not approved."


@pytest.mark.asyncio
async def test_apply_patch_tool_on_approval_callback_auto_approves() -> None:
    """Test that apply_patch tool on_approval callback can auto-approve."""

    async def needs_approval(_ctx, _operation, _call_id) -> bool:
        return True

    async def on_approval(
        _ctx: RunContextWrapper[Any], approval_item: ToolApprovalItem
    ) -> dict[str, Any]:
        return {"approve": True}

    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval, on_approval=on_approval)  # type: ignore[arg-type]  # type: ignore[arg-type]
    tool_call = DummyApplyPatchCall(
        type="apply_patch_call",
        call_id="call_apply",
        operation={"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"},
    )
    tool_run = ToolRunApplyPatchCall(tool_call=tool_call, apply_patch_tool=tool)
    agent = Agent(name="patcher", tools=[tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    # Should execute normally since on_approval auto-approved
    assert isinstance(result, ToolCallOutputItem)
    assert "Updated tasks.md" in result.output
    assert len(editor.operations) == 1


@pytest.mark.asyncio
async def test_apply_patch_tool_on_approval_callback_auto_rejects() -> None:
    """Test that apply_patch tool on_approval callback can auto-reject."""

    async def needs_approval(_ctx, _operation, _call_id) -> bool:
        return True

    async def on_approval(
        _ctx: RunContextWrapper[Any], approval_item: ToolApprovalItem
    ) -> dict[str, Any]:
        return {"approve": False, "reason": "Not allowed"}

    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval, on_approval=on_approval)  # type: ignore[arg-type]  # type: ignore[arg-type]
    tool_call = DummyApplyPatchCall(
        type="apply_patch_call",
        call_id="call_apply",
        operation={"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"},
    )
    tool_run = ToolRunApplyPatchCall(tool_call=tool_call, apply_patch_tool=tool)
    agent = Agent(name="patcher", tools=[tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    # Should return rejection output
    assert isinstance(result, ToolCallOutputItem)
    assert "Tool execution was not approved" in result.output
    assert len(editor.operations) == 0  # Should not have executed
