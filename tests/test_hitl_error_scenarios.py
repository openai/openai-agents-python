"""Tests for HITL error scenarios.

These tests are expected to fail initially and should pass after fixes are implemented.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from openai.types.responses import ResponseCustomToolCall, ResponseFunctionToolCall
from openai.types.responses.response_output_item import LocalShellCall
from pydantic_core import ValidationError

from agents import (
    Agent,
    ApplyPatchTool,
    LocalShellTool,
    Runner,
    RunState,
    ShellTool,
    ToolApprovalItem,
    function_tool,
)
from agents._run_impl import (
    NextStepInterruption,
)
from agents.run_context import RunContextWrapper
from agents.run_state import RunState as RunStateClass

from .fake_model import FakeModel
from .test_responses import get_text_message


class RecordingEditor:
    """Editor that records operations for testing."""

    def __init__(self) -> None:
        self.operations: list[Any] = []

    def create_file(self, operation: Any) -> Any:
        self.operations.append(operation)
        return {"output": f"Created {operation.path}", "status": "completed"}

    def update_file(self, operation: Any) -> Any:
        self.operations.append(operation)
        return {"output": f"Updated {operation.path}", "status": "completed"}

    def delete_file(self, operation: Any) -> Any:
        self.operations.append(operation)
        return {"output": f"Deleted {operation.path}", "status": "completed"}


@pytest.mark.asyncio
async def test_resumed_hitl_never_executes_approved_shell_tool():
    """Test that resumed HITL flow executes approved shell tools.

    After a shell tool is approved and the run is resumed, the shell tool should be
    executed and produce output. This test verifies that shell tool approvals work
    correctly during resumption.
    """
    model = FakeModel()

    async def needs_approval(_ctx, _action, _call_id) -> bool:
        return True

    shell_tool = ShellTool(executor=lambda request: "shell_output", needs_approval=needs_approval)
    agent = Agent(name="TestAgent", model=model, tools=[shell_tool])

    # First turn: model requests shell call requiring approval
    shell_call = cast(
        Any,
        {
            "type": "shell_call",
            "id": "shell_1",
            "call_id": "call_shell_1",
            "status": "in_progress",
            "action": {"type": "exec", "commands": ["echo test"], "timeout_ms": 1000},
        },
    )
    model.set_next_output([shell_call])

    result1 = await Runner.run(agent, "run shell command")
    assert result1.interruptions, "should have an interruption for shell tool approval"
    assert len(result1.interruptions) == 1
    assert isinstance(result1.interruptions[0], ToolApprovalItem)
    assert result1.interruptions[0].tool_name == "shell"

    # Approve the shell call
    state = result1.to_state()
    state.approve(result1.interruptions[0], always_approve=True)

    # Set up next model response (final output)
    model.set_next_output([get_text_message("done")])

    # Resume from state - should execute approved shell tool and produce output
    result2 = await Runner.run(agent, state)

    # The shell tool should have been executed and produced output
    # This test will fail because resolve_interrupted_turn doesn't execute shell calls
    shell_output_items = [
        item
        for item in result2.new_items
        if hasattr(item, "raw_item")
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "shell_call_output"
    ]
    assert len(shell_output_items) > 0, "Shell tool should have been executed after approval"
    assert any("shell_output" in str(item.output) for item in shell_output_items)


@pytest.mark.asyncio
async def test_resumed_hitl_never_executes_approved_apply_patch_tool():
    """Test that resumed HITL flow executes approved apply_patch tools.

    After an apply_patch tool is approved and the run is resumed, the apply_patch tool
    should be executed and produce output. This test verifies that apply_patch tool
    approvals work correctly during resumption.
    """
    model = FakeModel()
    editor = RecordingEditor()

    async def needs_approval(_ctx, _operation, _call_id) -> bool:
        return True

    apply_patch_tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval)
    agent = Agent(name="TestAgent", model=model, tools=[apply_patch_tool])

    # First turn: model requests apply_patch call requiring approval
    # Apply patch calls come from the model as ResponseCustomToolCall
    # The input is a JSON string containing the operation
    operation_json = json.dumps({"type": "update_file", "path": "test.md", "diff": "-a\n+b\n"})
    apply_patch_call = ResponseCustomToolCall(
        type="custom_tool_call",
        name="apply_patch",
        call_id="call_apply_1",
        input=operation_json,
    )
    model.set_next_output([apply_patch_call])

    result1 = await Runner.run(agent, "update file")
    assert result1.interruptions, "should have an interruption for apply_patch tool approval"
    assert len(result1.interruptions) == 1
    assert isinstance(result1.interruptions[0], ToolApprovalItem)
    assert result1.interruptions[0].tool_name == "apply_patch"

    # Approve the apply_patch call
    state = result1.to_state()
    state.approve(result1.interruptions[0], always_approve=True)

    # Set up next model response (final output)
    model.set_next_output([get_text_message("done")])

    # Resume from state - should execute approved apply_patch tool and produce output
    result2 = await Runner.run(agent, state)

    # The apply_patch tool should have been executed and produced output
    # This test will fail because resolve_interrupted_turn doesn't execute apply_patch calls
    apply_patch_output_items = [
        item
        for item in result2.new_items
        if hasattr(item, "raw_item")
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "apply_patch_call_output"
    ]
    assert len(apply_patch_output_items) > 0, (
        "ApplyPatch tool should have been executed after approval"
    )
    assert len(editor.operations) > 0, "Editor should have been called"


@pytest.mark.asyncio
async def test_resuming_pending_mcp_approvals_raises_typeerror():
    """Test that ToolApprovalItem can be added to a set (should be hashable).

    At line 783 in _run_impl.py, resolve_interrupted_turn tries:
        pending_hosted_mcp_approvals.add(approval_item)
    where approval_item is a ToolApprovalItem. This currently raises TypeError because
    ToolApprovalItem is not hashable.

    BUG: ToolApprovalItem lacks __hash__, so line 783 will raise TypeError.
    This test will FAIL with TypeError when the bug exists, and PASS when fixed.
    """
    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, tools=[])

    # Create a ToolApprovalItem - this is what line 783 tries to add to a set
    mcp_raw_item = {
        "type": "hosted_tool_call",
        "id": "mcp-approval-1",
        "name": "test_mcp_tool",
    }
    mcp_approval_item = ToolApprovalItem(
        agent=agent, raw_item=mcp_raw_item, tool_name="test_mcp_tool"
    )

    # BUG: This will raise TypeError because ToolApprovalItem is not hashable
    # This is exactly what happens at line 783: pending_hosted_mcp_approvals.add(approval_item)
    pending_hosted_mcp_approvals: set[ToolApprovalItem] = set()
    pending_hosted_mcp_approvals.add(
        mcp_approval_item
    )  # Should work once ToolApprovalItem is hashable
    assert mcp_approval_item in pending_hosted_mcp_approvals


@pytest.mark.asyncio
async def test_route_local_shell_calls_to_remote_shell_tool():
    """Test that local shell calls are routed to the local shell tool.

    When processing model output with LocalShellCall items, they should be handled by
    LocalShellTool (not ShellTool), even when both tools are registered. This ensures
    local shell operations use the correct executor and approval hooks.
    """
    model = FakeModel()

    remote_shell_executed = []
    local_shell_executed = []

    def remote_executor(request: Any) -> str:
        remote_shell_executed.append(request)
        return "remote_output"

    def local_executor(request: Any) -> str:
        local_shell_executed.append(request)
        return "local_output"

    shell_tool = ShellTool(executor=remote_executor)
    local_shell_tool = LocalShellTool(executor=local_executor)
    agent = Agent(name="TestAgent", model=model, tools=[shell_tool, local_shell_tool])

    # Model emits a local_shell_call
    local_shell_call = LocalShellCall(
        id="local_1",
        call_id="call_local_1",
        type="local_shell_call",
        action={"type": "exec", "command": ["echo", "test"], "env": {}},  # type: ignore[arg-type]
        status="in_progress",
    )
    model.set_next_output([local_shell_call])

    await Runner.run(agent, "run local shell")

    # Local shell call should be handled by LocalShellTool, not ShellTool
    # This test will fail because LocalShellCall is routed to shell_tool first
    assert len(local_shell_executed) > 0, "LocalShellTool should have been executed"
    assert len(remote_shell_executed) == 0, (
        "ShellTool should not have been executed for local shell call"
    )


@pytest.mark.asyncio
async def test_preserve_max_turns_when_resuming_from_runresult_state():
    """Test that max_turns is preserved when resuming from RunResult state.

    When a run configured with max_turns=20 is interrupted and resumed via
    result.to_state() without re-passing max_turns, the resumed run should continue
    with the original max_turns value (20), not default back to 10.
    """
    model = FakeModel()

    async def test_tool() -> str:
        return "tool_result"

    async def needs_approval(_ctx, _params, _call_id) -> bool:
        return True

    # Create the tool with needs_approval directly
    # The tool name will be "test_tool" based on the function name
    tool = function_tool(test_tool, needs_approval=needs_approval)
    agent = Agent(name="TestAgent", model=model, tools=[tool])

    # Configure run with max_turns=20
    # First turn: tool call requiring approval (interruption)
    model.add_multiple_turn_outputs(
        [
            [
                cast(
                    ResponseFunctionToolCall,
                    {
                        "type": "function_call",
                        "name": "test_tool",
                        "call_id": "call-1",
                        "arguments": "{}",
                    },
                )
            ],
        ]
    )

    result1 = await Runner.run(agent, "call test_tool", max_turns=20)
    assert result1.interruptions, "should have an interruption"
    # After first turn with interruption, we're at turn 1

    # Approve and resume without re-passing max_turns
    state = result1.to_state()
    state.approve(result1.interruptions[0], always_approve=True)

    # Set up enough turns to exceed 10 (the hardcoded default) but stay under 20
    # (the original max_turns)
    # After first turn with interruption, current_turn=1 in state
    # When resuming, current_turn is restored from state (1),
    # then resolve_interrupted_turn is called
    # If NextStepRunAgain, loop continues, then current_turn is incremented
    # (becomes 2), then model is called
    # With max_turns=10, we can do turns 2-10 (9 more turns), so turn 11 would exceed limit
    # BUG: max_turns defaults to 10 when resuming (not pulled from state)
    # We need 10 more turns after resolving interruption to exceed limit (turns 2-11)
    # Pattern from test_max_turns.py: text message first, then tool call (both in same response)
    # This ensures the model continues (doesn't finish) and calls the tool, triggering another turn
    # After resolving interruption, the model is called again, so we need responses for turns 2-11
    # IMPORTANT: After resolving, if NextStepRunAgain, the loop continues WITHOUT incrementing turn
    # Then the normal flow starts, which increments turn to 2, then calls the model
    # So we need 10 model responses to get turns 2-11
    model.add_multiple_turn_outputs(
        [
            [
                get_text_message(f"turn {i + 2}"),  # Text message first (doesn't finish)
                cast(
                    ResponseFunctionToolCall,
                    {
                        "type": "function_call",
                        "name": "test_tool",
                        "call_id": f"call-{i + 2}",
                        "arguments": "{}",
                    },
                ),
            ]
            for i in range(
                10
            )  # 10 more tool calls = 10 more turns (turns 2-11, exceeding limit of 10 at turn 11)
        ]
    )

    # Resume without passing max_turns - should use 20 from state (not default to 10)
    # BUG: Runner.run doesn't pull max_turns from state, so it defaults to 10.
    # With max_turns=10 and current_turn=1, we can do turns 2-10 (9 more),
    # but we're trying to do 10 more turns (turns 2-11),
    # so turn 11 > max_turns (10) should raise MaxTurnsExceeded
    # This test checks for CORRECT behavior (max_turns preserved)
    # and will FAIL when the bug exists.
    # BUG EXISTS: MaxTurnsExceeded should be raised when max_turns defaults to 10,
    # but we want max_turns=20

    # When the bug exists, MaxTurnsExceeded WILL be raised
    # (because max_turns defaults to 10)
    # When the bug is fixed, MaxTurnsExceeded will NOT be raised
    # (because max_turns will be 20 from state)
    # So we should assert that the run succeeds WITHOUT raising MaxTurnsExceeded
    result2 = await Runner.run(agent, state)
    # If we get here without MaxTurnsExceeded, the bug is fixed (max_turns was preserved as 20)
    # If MaxTurnsExceeded was raised, the bug exists (max_turns defaulted to 10)
    assert result2 is not None, "Run should complete successfully with max_turns=20 from state"


@pytest.mark.asyncio
async def test_current_turn_not_preserved_in_to_state():
    """Test that current turn counter is preserved when converting RunResult to RunState.

    When a run is interrupted after one or more turns and converted to state via result.to_state(),
    the current turn counter should be preserved. This ensures:
    1. Turn numbers are reported correctly in resumed execution
    2. max_turns enforcement works correctly across resumption

    BUG: to_state() initializes RunState with _current_turn=0 instead of preserving
    the actual current turn from the result.
    """
    model = FakeModel()

    async def test_tool() -> str:
        return "tool_result"

    async def needs_approval(_ctx, _params, _call_id) -> bool:
        return True

    tool = function_tool(test_tool, needs_approval=needs_approval)
    agent = Agent(name="TestAgent", model=model, tools=[tool])

    # Model emits a tool call requiring approval
    model.set_next_output(
        [
            cast(
                ResponseFunctionToolCall,
                {
                    "type": "function_call",
                    "name": "test_tool",
                    "call_id": "call-1",
                    "arguments": "{}",
                },
            )
        ]
    )

    # First turn with interruption
    result1 = await Runner.run(agent, "call test_tool")
    assert result1.interruptions, "should have interruption on turn 1"

    # Convert to state - this should preserve current_turn=1
    state1 = result1.to_state()

    # BUG: state1._current_turn should be 1, but to_state() resets it to 0
    # This will fail when the bug exists
    assert state1._current_turn == 1, (
        f"Expected current_turn=1 after 1 turn, got {state1._current_turn}. "
        "to_state() should preserve the current turn counter."
    )


@pytest.mark.asyncio
async def test_deserialize_only_function_approvals_breaks_hitl_for_other_tools():
    """Test that deserialization correctly reconstructs shell tool approvals.

    When restoring a run from JSON with shell tool approvals, the interruption should be
    correctly reconstructed and preserve the shell tool type (not converted to function call).
    """
    model = FakeModel()

    async def needs_approval(_ctx, _action, _call_id) -> bool:
        return True

    shell_tool = ShellTool(executor=lambda request: "output", needs_approval=needs_approval)
    agent = Agent(name="TestAgent", model=model, tools=[shell_tool])

    # First turn: shell call requiring approval
    shell_call = cast(
        Any,
        {
            "type": "shell_call",
            "id": "shell_1",
            "call_id": "call_shell_1",
            "status": "in_progress",
            "action": {"type": "exec", "commands": ["echo test"], "timeout_ms": 1000},
        },
    )
    model.set_next_output([shell_call])

    result1 = await Runner.run(agent, "run shell")
    assert result1.interruptions, "should have interruption"

    # Serialize state to JSON
    state = result1.to_state()
    state_json = state.to_json()

    # Deserialize from JSON - this should succeed and correctly reconstruct
    # shell approval
    # BUG: from_json tries to create ResponseFunctionToolCall from shell call
    # and raises ValidationError
    # When the bug exists, ValidationError will be raised
    # When fixed, deserialization should succeed
    try:
        deserialized_state = await RunStateClass.from_json(agent, state_json)
        # The interruption should be correctly reconstructed
        interruptions = deserialized_state.get_interruptions()
        assert len(interruptions) > 0, "Interruptions should be preserved after deserialization"
        # The interruption should be for shell, not function
        assert interruptions[0].tool_name == "shell", (
            "Shell tool approval should be preserved, not converted to function"
        )
    except ValidationError as e:
        # BUG EXISTS: ValidationError raised because from_json assumes
        # all interruptions are function calls
        pytest.fail(
            f"BUG: Deserialization failed with ValidationError - "
            f"from_json assumes all interruptions are function tool calls, "
            f"but this is a shell tool approval. Error: {e}"
        )


@pytest.mark.asyncio
async def test_deserialize_only_function_approvals_breaks_hitl_for_apply_patch_tools():
    """Test that deserialization correctly reconstructs apply_patch tool approvals.

    When restoring a run from JSON with apply_patch tool approvals, the interruption should
    be correctly reconstructed and preserve the apply_patch tool type (not converted to
    function call).
    """
    model = FakeModel()

    async def needs_approval(_ctx, _operation, _call_id) -> bool:
        return True

    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval)
    agent = Agent(name="TestAgent", model=model, tools=[apply_patch_tool])

    # First turn: apply_patch call requiring approval
    apply_patch_call = cast(
        Any,
        {
            "type": "apply_patch_call",
            "call_id": "call_apply_1",
            "operation": {"type": "update_file", "path": "test.md", "diff": "-a\n+b\n"},
        },
    )
    model.set_next_output([apply_patch_call])

    result1 = await Runner.run(agent, "update file")
    assert result1.interruptions, "should have interruption"

    # Serialize state to JSON
    state = result1.to_state()
    state_json = state.to_json()

    # Deserialize from JSON - this should succeed and correctly reconstruct
    # apply_patch approval
    # BUG: from_json tries to create ResponseFunctionToolCall from
    # apply_patch call and raises ValidationError
    # When the bug exists, ValidationError will be raised
    # When fixed, deserialization should succeed
    try:
        deserialized_state = await RunStateClass.from_json(agent, state_json)
        # The interruption should be correctly reconstructed
        interruptions = deserialized_state.get_interruptions()
        assert len(interruptions) > 0, "Interruptions should be preserved after deserialization"
        # The interruption should be for apply_patch, not function
        assert interruptions[0].tool_name == "apply_patch", (
            "ApplyPatch tool approval should be preserved, not converted to function"
        )
    except ValidationError as e:
        # BUG EXISTS: ValidationError raised because from_json assumes
        # all interruptions are function calls
        pytest.fail(
            f"BUG: Deserialization failed with ValidationError - "
            f"from_json assumes all interruptions are function tool calls, "
            f"but this is an apply_patch tool approval. Error: {e}"
        )


@pytest.mark.asyncio
async def test_deserialize_only_function_approvals_breaks_hitl_for_mcp_tools():
    """Test that deserialization correctly reconstructs MCP tool approvals.

    When restoring a run from JSON with MCP/hosted tool approvals, the interruption should
    be correctly reconstructed and preserve the MCP tool type (not converted to function call).
    """
    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, tools=[])

    # Create a state with a ToolApprovalItem interruption containing an MCP-related raw_item
    # This simulates a scenario where an MCP approval was somehow wrapped in a ToolApprovalItem
    # (which could happen in edge cases or future code changes)
    mcp_raw_item = {
        "type": "hosted_tool_call",
        "name": "test_mcp_tool",
        "call_id": "call_mcp_1",
        "providerData": {
            "type": "mcp_approval_request",
            "id": "req-1",
            "server_label": "test_server",
        },
    }
    mcp_approval_item = ToolApprovalItem(
        agent=agent, raw_item=mcp_raw_item, tool_name="test_mcp_tool"
    )

    # Create a state with this interruption
    context: RunContextWrapper = RunContextWrapper(context={})
    state = RunState(
        context=context,
        original_input="test",
        starting_agent=agent,
        max_turns=10,
    )
    state._current_step = NextStepInterruption(interruptions=[mcp_approval_item])

    # Serialize state to JSON
    state_json = state.to_json()

    # Deserialize from JSON - this should succeed and correctly reconstruct
    # MCP approval
    # BUG: from_json tries to create ResponseFunctionToolCall from
    # the MCP raw_item (hosted_tool_call type), which doesn't match the schema
    # and raises ValidationError
    # When the bug exists, ValidationError will be raised
    # When fixed, deserialization should succeed
    try:
        deserialized_state = await RunStateClass.from_json(agent, state_json)
        # The interruption should be correctly reconstructed
        interruptions = deserialized_state.get_interruptions()
        assert len(interruptions) > 0, "Interruptions should be preserved after deserialization"
        # The interruption should be for MCP, not function
        assert interruptions[0].tool_name == "test_mcp_tool", (
            "MCP tool approval should be preserved, not converted to function"
        )
    except ValidationError as e:
        # BUG EXISTS: ValidationError raised because from_json assumes
        # all interruptions are function calls
        pytest.fail(
            f"BUG: Deserialization failed with ValidationError - "
            f"from_json assumes all interruptions are function tool calls, "
            f"but this is an MCP/hosted tool approval. Error: {e}"
        )


@pytest.mark.asyncio
async def test_deserializing_interruptions_assumes_function_tool_calls():
    """Test that deserializing interruptions preserves apply_patch tool calls.

    When resuming a saved RunState with apply_patch tool approvals, deserialization should
    correctly reconstruct the interruption without forcing it to a function call type.
    """
    model = FakeModel()

    async def needs_approval(_ctx, _operation, _call_id) -> bool:
        return True

    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval)
    agent = Agent(name="TestAgent", model=model, tools=[apply_patch_tool])

    # First turn: apply_patch call requiring approval
    apply_patch_call = cast(
        Any,
        {
            "type": "apply_patch_call",
            "call_id": "call_apply_1",
            "operation": {"type": "update_file", "path": "test.md", "diff": "-a\n+b\n"},
        },
    )
    model.set_next_output([apply_patch_call])

    result1 = await Runner.run(agent, "update file")
    assert result1.interruptions, "should have interruption"

    # Serialize state to JSON
    state = result1.to_state()
    state_json = state.to_json()

    # Deserialize from JSON - this should succeed and correctly reconstruct
    # apply_patch approval
    # BUG: from_json tries to create ResponseFunctionToolCall from
    # apply_patch call and raises ValidationError
    # When the bug exists, ValidationError will be raised
    # When fixed, deserialization should succeed
    try:
        deserialized_state = await RunStateClass.from_json(agent, state_json)
        # The interruption should be correctly reconstructed
        interruptions = deserialized_state.get_interruptions()
        assert len(interruptions) > 0, "Interruptions should be preserved after deserialization"
        # The interruption should be for apply_patch, not function
        assert interruptions[0].tool_name == "apply_patch", (
            "ApplyPatch tool approval should be preserved, not converted to function"
        )
    except ValidationError as e:
        # BUG EXISTS: ValidationError raised because from_json assumes
        # all interruptions are function calls
        pytest.fail(
            f"BUG: Deserialization failed with ValidationError - "
            f"from_json assumes all interruptions are function tool calls, "
            f"but this is an apply_patch tool approval. Error: {e}"
        )


@pytest.mark.asyncio
async def test_deserializing_interruptions_assumes_function_tool_calls_shell():
    """Test that deserializing interruptions preserves shell tool calls.

    When resuming a saved RunState with shell tool approvals, deserialization should
    correctly reconstruct the interruption without forcing it to a function call type.
    """
    model = FakeModel()

    async def needs_approval(_ctx, _action, _call_id) -> bool:
        return True

    shell_tool = ShellTool(executor=lambda request: "output", needs_approval=needs_approval)
    agent = Agent(name="TestAgent", model=model, tools=[shell_tool])

    # First turn: shell call requiring approval
    shell_call = cast(
        Any,
        {
            "type": "shell_call",
            "id": "shell_1",
            "call_id": "call_shell_1",
            "status": "in_progress",
            "action": {"type": "exec", "commands": ["echo test"], "timeout_ms": 1000},
        },
    )
    model.set_next_output([shell_call])

    result1 = await Runner.run(agent, "run shell")
    assert result1.interruptions, "should have interruption"

    # Serialize state to JSON
    state = result1.to_state()
    state_json = state.to_json()

    # Deserialize from JSON - this should succeed and correctly reconstruct
    # shell approval
    # BUG: from_json tries to create ResponseFunctionToolCall from shell call
    # and raises ValidationError
    # When the bug exists, ValidationError will be raised
    # When fixed, deserialization should succeed
    try:
        deserialized_state = await RunStateClass.from_json(agent, state_json)
        # The interruption should be correctly reconstructed
        interruptions = deserialized_state.get_interruptions()
        assert len(interruptions) > 0, "Interruptions should be preserved after deserialization"
        # The interruption should be for shell, not function
        assert interruptions[0].tool_name == "shell", (
            "Shell tool approval should be preserved, not converted to function"
        )
    except ValidationError as e:
        # BUG EXISTS: ValidationError raised because from_json assumes
        # all interruptions are function calls
        pytest.fail(
            f"BUG: Deserialization failed with ValidationError - "
            f"from_json assumes all interruptions are function tool calls, "
            f"but this is a shell tool approval. Error: {e}"
        )


@pytest.mark.asyncio
async def test_deserializing_interruptions_assumes_function_tool_calls_mcp():
    """Test that deserializing interruptions preserves MCP/hosted tool calls.

    When resuming a saved RunState with MCP/hosted tool approvals, deserialization should
    correctly reconstruct the interruption without forcing it to a function call type.
    """
    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, tools=[])

    # Create a state with a ToolApprovalItem interruption containing an MCP-related raw_item
    # This simulates a scenario where an MCP approval was somehow wrapped in a ToolApprovalItem
    # (which could happen in edge cases or future code changes)
    mcp_raw_item = {
        "type": "hosted_tool_call",
        "name": "test_mcp_tool",
        "call_id": "call_mcp_1",
        "providerData": {
            "type": "mcp_approval_request",
            "id": "req-1",
            "server_label": "test_server",
        },
    }
    mcp_approval_item = ToolApprovalItem(
        agent=agent, raw_item=mcp_raw_item, tool_name="test_mcp_tool"
    )

    # Create a state with this interruption
    context: RunContextWrapper = RunContextWrapper(context={})
    state = RunState(
        context=context,
        original_input="test",
        starting_agent=agent,
        max_turns=10,
    )
    state._current_step = NextStepInterruption(interruptions=[mcp_approval_item])

    # Serialize state to JSON
    state_json = state.to_json()

    # Deserialize from JSON - this should succeed and correctly reconstruct
    # MCP approval
    # BUG: from_json tries to create ResponseFunctionToolCall from
    # the MCP raw_item (hosted_tool_call type), which doesn't match the schema
    # and raises ValidationError
    # When the bug exists, ValidationError will be raised
    # When fixed, deserialization should succeed
    try:
        deserialized_state = await RunStateClass.from_json(agent, state_json)
        # The interruption should be correctly reconstructed
        interruptions = deserialized_state.get_interruptions()
        assert len(interruptions) > 0, "Interruptions should be preserved after deserialization"
        # The interruption should be for MCP, not function
        assert interruptions[0].tool_name == "test_mcp_tool", (
            "MCP tool approval should be preserved, not converted to function"
        )
    except ValidationError as e:
        # BUG EXISTS: ValidationError raised because from_json assumes
        # all interruptions are function calls
        pytest.fail(
            f"BUG: Deserialization failed with ValidationError - "
            f"from_json assumes all interruptions are function tool calls, "
            f"but this is an MCP/hosted tool approval. Error: {e}"
        )
