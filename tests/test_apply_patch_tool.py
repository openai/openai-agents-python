from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import pytest

from agents import (
    Agent,
    ApplyPatchTool,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    set_tracing_disabled,
    trace,
)
from agents.editor import ApplyPatchOperation, ApplyPatchResult
from agents.items import ToolApprovalItem, ToolCallOutputItem
from agents.run_internal.run_loop import ApplyPatchAction, ToolRunApplyPatchCall

from .testing_processor import SPAN_PROCESSOR_TESTING
from .utils.hitl import (
    HITL_REJECTION_MSG,
    make_context_wrapper,
    make_on_approval_callback,
    reject_tool_call,
    require_approval,
)


def _get_function_span(tool_name: str) -> dict[str, Any]:
    for span in SPAN_PROCESSOR_TESTING.get_ordered_spans(including_empty=True):
        exported = span.export()
        if not exported:
            continue
        span_data = exported.get("span_data")
        if not isinstance(span_data, dict):
            continue
        if span_data.get("type") == "function" and span_data.get("name") == tool_name:
            return exported
    raise AssertionError(f"Function span for tool '{tool_name}' not found")


def _call(call_id: str, operation: dict[str, Any]) -> DummyApplyPatchCall:
    return DummyApplyPatchCall(type="apply_patch_call", call_id=call_id, operation=operation)


def build_apply_patch_call(
    tool: ApplyPatchTool,
    call_id: str,
    operation: dict[str, Any],
    *,
    context_wrapper: RunContextWrapper[Any] | None = None,
) -> tuple[Agent[Any], RunContextWrapper[Any], ToolRunApplyPatchCall]:
    ctx = context_wrapper or make_context_wrapper()
    agent = Agent(name="patcher", tools=[tool])
    tool_run = ToolRunApplyPatchCall(tool_call=_call(call_id, operation), apply_patch_tool=tool)
    return agent, ctx, tool_run


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
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool, "call_apply", {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"}
    )

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
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool, "call_apply_fail", {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"}
    )

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
async def test_apply_patch_tool_emits_function_span() -> None:
    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor)
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool, "call_apply_trace", {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"}
    )

    set_tracing_disabled(False)
    with trace("apply-patch-span-test"):
        result = await ApplyPatchAction.execute(
            agent=agent,
            call=tool_run,
            hooks=RunHooks[Any](),
            context_wrapper=context_wrapper,
            config=RunConfig(),
        )

    assert isinstance(result, ToolCallOutputItem)
    function_span = _get_function_span(tool.name)
    span_data = cast(dict[str, Any], function_span["span_data"])
    assert "tasks.md" in cast(str, span_data.get("input", ""))
    assert "Updated tasks.md" in cast(str, span_data.get("output", ""))


@pytest.mark.asyncio
async def test_apply_patch_tool_redacts_span_error_when_sensitive_data_disabled() -> None:
    secret_error = "patch secret output"

    class ExplodingEditor(RecordingEditor):
        def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
            raise RuntimeError(secret_error)

    tool = ApplyPatchTool(editor=ExplodingEditor())
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool,
        "call_apply_trace_redacted",
        {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"},
    )

    set_tracing_disabled(False)
    with trace("apply-patch-span-redaction-test"):
        result = await ApplyPatchAction.execute(
            agent=agent,
            call=tool_run,
            hooks=RunHooks[Any](),
            context_wrapper=context_wrapper,
            config=RunConfig(trace_include_sensitive_data=False),
        )

    assert isinstance(result, ToolCallOutputItem)
    function_span = _get_function_span(tool.name)
    assert function_span.get("error") == {
        "message": "Error running tool",
        "data": {
            "tool_name": tool.name,
            "error": "Tool execution failed. Error details are redacted.",
        },
    }
    assert secret_error not in json.dumps(function_span)
    span_data = cast(dict[str, Any], function_span["span_data"])
    assert span_data.get("input") is None
    assert span_data.get("output") is None


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
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool,
        "call_mapping",
        tool_call["operation"],
        context_wrapper=RunContextWrapper(context=None),
    )

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

    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor, needs_approval=require_approval)
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool, "call_apply", {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"}
    )

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolApprovalItem)
    assert result.tool_name == "apply_patch"
    assert result.name == "apply_patch"


@pytest.mark.asyncio
async def test_apply_patch_tool_needs_approval_rejected_returns_rejection() -> None:
    """Test that apply_patch tool with needs_approval that is rejected returns rejection output."""

    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor, needs_approval=require_approval)
    tool_call = _call("call_apply", {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"})
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool, "call_apply", tool_call.operation, context_wrapper=make_context_wrapper()
    )

    # Pre-reject the tool call
    reject_tool_call(context_wrapper, agent, cast(dict[str, Any], tool_call), "apply_patch")

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert HITL_REJECTION_MSG in result.output
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["type"] == "apply_patch_call_output"
    assert raw_item["status"] == "failed"
    assert raw_item["output"] == HITL_REJECTION_MSG


@pytest.mark.asyncio
async def test_apply_patch_rejection_uses_run_level_formatter() -> None:
    """Apply patch approval rejection should use the run-level formatter message."""

    editor = RecordingEditor()
    tool = ApplyPatchTool(
        editor=editor,
        needs_approval=require_approval,
    )
    tool_call = _call("call_apply", {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"})
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool, "call_apply", tool_call.operation, context_wrapper=make_context_wrapper()
    )

    reject_tool_call(context_wrapper, agent, cast(dict[str, Any], tool_call), "apply_patch")

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(
            tool_error_formatter=lambda args: f"{args.tool_name} denied ({args.call_id})"
        ),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert result.output == "apply_patch denied (call_apply)"
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["output"] == "apply_patch denied (call_apply)"


@pytest.mark.asyncio
async def test_apply_patch_tool_on_approval_callback_auto_approves() -> None:
    """Test that apply_patch tool on_approval callback can auto-approve."""

    editor = RecordingEditor()
    tool = ApplyPatchTool(
        editor=editor,
        needs_approval=require_approval,
        on_approval=make_on_approval_callback(approve=True),
    )
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool, "call_apply", {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"}
    )

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

    editor = RecordingEditor()
    tool = ApplyPatchTool(
        editor=editor,
        needs_approval=require_approval,
        on_approval=make_on_approval_callback(approve=False, reason="Not allowed"),
    )
    agent, context_wrapper, tool_run = build_apply_patch_call(
        tool, "call_apply", {"type": "update_file", "path": "tasks.md", "diff": "-a\n+b\n"}
    )

    result = await ApplyPatchAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    # Should return rejection output
    assert isinstance(result, ToolCallOutputItem)
    assert HITL_REJECTION_MSG in result.output
    assert len(editor.operations) == 0  # Should not have executed
