"""Tests for HITL error scenarios.

These tests are expected to fail initially and should pass after fixes are implemented.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from openai.types.responses import ResponseCustomToolCall, ResponseFunctionToolCall
from openai.types.responses.response_input_param import (
    ComputerCallOutput,
    LocalShellCallOutput,
)
from openai.types.responses.response_output_item import LocalShellCall
from pydantic_core import ValidationError

from agents import (
    Agent,
    ApplyPatchTool,
    LocalShellTool,
    RunConfig,
    RunHooks,
    Runner,
    RunState,
    ShellTool,
    ToolApprovalItem,
    function_tool,
)
from agents._run_impl import (
    NextStepInterruption,
    ProcessedResponse,
    RunImpl,
    ToolRunFunction,
    ToolRunShellCall,
)
from agents.items import MessageOutputItem, ModelResponse, ToolCallOutputItem
from agents.run_context import RunContextWrapper
from agents.run_state import RunState as RunStateClass
from agents.usage import Usage

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message
from .utils.simple_session import SimpleListSession


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

    In resolve_interrupted_turn, the code tries:
        pending_hosted_mcp_approvals.add(approval_item)
    where approval_item is a ToolApprovalItem. This currently raises TypeError because
    ToolApprovalItem is not hashable.

    BUG: ToolApprovalItem lacks __hash__, so adding it to a set will raise TypeError.
    This test will FAIL with TypeError when the bug exists, and PASS when fixed.
    """
    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, tools=[])

    # Create a ToolApprovalItem - this is what the code tries to add to a set
    mcp_raw_item = {
        "type": "hosted_tool_call",
        "id": "mcp-approval-1",
        "name": "test_mcp_tool",
    }
    mcp_approval_item = ToolApprovalItem(
        agent=agent, raw_item=mcp_raw_item, tool_name="test_mcp_tool"
    )

    # BUG: This will raise TypeError because ToolApprovalItem is not hashable
    # This is exactly what happens: pending_hosted_mcp_approvals.add(approval_item)
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


@pytest.mark.asyncio
async def test_preserve_persisted_item_counter_when_resuming_streamed_runs():
    """Test that persisted-item counter is preserved when resuming streamed runs.

    When constructing RunResultStreaming from a RunState, _current_turn_persisted_item_count
    should be preserved from the state, not reset to len(run_state._generated_items). This is
    critical for Python-to-Python resumes where the counter accurately reflects how many items
    were actually persisted before the interruption.

    BUG: When run_state._generated_items is truthy, the code always sets
    _current_turn_persisted_item_count to len(run_state._generated_items), overriding the actual
    persisted count saved in the state. This causes missing history in sessions when a turn was
    interrupted mid-persistence (e.g., 5 items generated but only 3 persisted).
    """
    model = FakeModel()
    agent = Agent(name="TestAgent", model=model)

    # Create a RunState with 5 generated items but only 3 persisted
    # This simulates a scenario where a turn was interrupted mid-persistence:
    # - 5 items were generated
    # - Only 3 items were persisted to the session before interruption
    # - The state correctly tracks _current_turn_persisted_item_count=3
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})
    state = RunState(
        context=context_wrapper,
        original_input="test input",
        starting_agent=agent,
        max_turns=10,
    )

    # Create 5 generated items (simulating multiple outputs before interruption)
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    for i in range(5):
        message_item = MessageOutputItem(
            agent=agent,
            raw_item=ResponseOutputMessage(
                id=f"msg_{i}",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(
                        type="output_text", text=f"Message {i}", annotations=[], logprobs=[]
                    )
                ],
            ),
        )
        state._generated_items.append(message_item)

    # Set the persisted count to 3 (only 3 items were persisted before interruption)
    state._current_turn_persisted_item_count = 3

    # Add a model response so the state is valid for resumption
    state._model_responses = [
        ModelResponse(
            output=[get_text_message("test")],
            usage=Usage(),
            response_id="resp_1",
        )
    ]

    # Set up model to return final output immediately (so the run completes)
    model.set_next_output([get_text_message("done")])

    # Resume from state using run_streamed
    # BUG: When constructing RunResultStreaming, the code will incorrectly set
    # _current_turn_persisted_item_count to len(_generated_items)=5 instead of preserving
    # the actual persisted count of 3
    result = Runner.run_streamed(agent, state)

    # The persisted count should be preserved as 3, not reset to 5
    # This test will FAIL when the bug exists (count will be 5)
    # and PASS when fixed (count will be 3)
    assert result._current_turn_persisted_item_count == 3, (
        f"Expected _current_turn_persisted_item_count=3 (the actual persisted count), "
        f"but got {result._current_turn_persisted_item_count}. "
        f"The bug incorrectly resets the counter to "
        f"len(run_state._generated_items)={len(state._generated_items)} instead of "
        f"preserving the actual persisted count from the state. This causes missing "
        f"history in sessions when resuming after mid-persistence interruptions."
    )

    # Consume events to complete the run
    async for _ in result.stream_events():
        pass


@pytest.mark.asyncio
async def test_preserve_tool_output_types_during_serialization():
    """Test that tool output types are preserved during run state serialization.

    When serializing a run state, `_convert_output_item_to_protocol` unconditionally
    overwrites every tool output's `type` with `function_call_result`. On restore,
    `_deserialize_items` dispatches on this `type` to choose between
    `FunctionCallOutput`, `ComputerCallOutput`, or `LocalShellCallOutput`, so
    computer/shell/apply_patch outputs that were originally
    `computer_call_output`/`local_shell_call_output` are rehydrated as
    `function_call_output` (or fail validation), losing the tool-specific payload
    and breaking resumption for those tools.

    This test will FAIL when the bug exists (output type will be function_call_result)
    and PASS when fixed (output type will be preserved as computer_call_output or
    local_shell_call_output).
    """

    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, tools=[])

    # Create a RunState with a computer tool output
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    state = RunState(context=context, original_input="test", starting_agent=agent, max_turns=3)

    # Create a computer_call_output item
    computer_output: ComputerCallOutput = {
        "type": "computer_call_output",
        "call_id": "call_computer_1",
        "output": {"type": "computer_screenshot", "image_url": "base64_screenshot_data"},
    }
    computer_output_item = ToolCallOutputItem(
        agent=agent,
        raw_item=computer_output,
        output="screenshot_data",
    )
    state._generated_items = [computer_output_item]

    # Serialize and deserialize the state
    json_data = state.to_json()

    # Check what was serialized - the bug converts computer_call_output to function_call_result
    generated_items_json = json_data.get("generatedItems", [])
    assert len(generated_items_json) == 1, "Computer output item should be serialized"
    raw_item_json = generated_items_json[0].get("rawItem", {})
    serialized_type = raw_item_json.get("type")

    # The bug: _convert_output_item_to_protocol converts all tool outputs to function_call_result
    # This test will FAIL when the bug exists (type will be function_call_result)
    # and PASS when fixed (type will be computer_call_output)
    assert serialized_type == "computer_call_output", (
        f"Expected computer_call_output in serialized JSON, but got {serialized_type}. "
        f"The bug in _convert_output_item_to_protocol converts all tool outputs to "
        f"function_call_result during serialization, causing them to be incorrectly "
        f"deserialized as FunctionCallOutput instead of ComputerCallOutput."
    )

    deserialized_state = await RunStateClass.from_json(agent, json_data)

    # Verify that the computer output type is preserved after deserialization
    # When the bug exists, the item may be skipped due to validation errors
    # When fixed, it should deserialize correctly
    assert len(deserialized_state._generated_items) == 1, (
        "Computer output item should be deserialized. When the bug exists, it may be skipped "
        "due to validation errors when trying to deserialize as FunctionCallOutput instead "
        "of ComputerCallOutput."
    )
    deserialized_item = deserialized_state._generated_items[0]
    assert isinstance(deserialized_item, ToolCallOutputItem)

    # The raw_item should still be a ComputerCallOutput, not FunctionCallOutput
    raw_item = deserialized_item.raw_item
    if isinstance(raw_item, dict):
        output_type = raw_item.get("type")
        assert output_type == "computer_call_output", (
            f"Expected computer_call_output, but got {output_type}. "
            f"The bug converts all tool outputs to function_call_result during serialization, "
            f"causing them to be incorrectly deserialized as FunctionCallOutput."
        )
    else:
        # If it's a Pydantic model, check the type attribute
        assert hasattr(raw_item, "type")
        assert raw_item.type == "computer_call_output", (
            f"Expected computer_call_output, but got {raw_item.type}. "
            f"The bug converts all tool outputs to function_call_result during serialization, "
            f"causing them to be incorrectly deserialized as FunctionCallOutput."
        )

    # Test with local_shell_call_output as well
    # Note: The TypedDict definition requires "id" but runtime uses "call_id"
    # We use cast to match the actual runtime structure
    shell_output = cast(
        LocalShellCallOutput,
        {
            "type": "local_shell_call_output",
            "id": "shell_1",
            "call_id": "call_shell_1",
            "output": "command output",
        },
    )
    shell_output_item = ToolCallOutputItem(
        agent=agent,
        raw_item=shell_output,
        output="command output",
    )
    state._generated_items = [shell_output_item]

    # Serialize and deserialize again
    json_data = state.to_json()

    # Check what was serialized - the bug converts local_shell_call_output to function_call_result
    generated_items_json = json_data.get("generatedItems", [])
    assert len(generated_items_json) == 1, "Shell output item should be serialized"
    raw_item_json = generated_items_json[0].get("rawItem", {})
    serialized_type = raw_item_json.get("type")

    # The bug: _convert_output_item_to_protocol converts all tool outputs to function_call_result
    # This test will FAIL when the bug exists (type will be function_call_result)
    # and PASS when fixed (type will be local_shell_call_output)
    assert serialized_type == "local_shell_call_output", (
        f"Expected local_shell_call_output in serialized JSON, but got {serialized_type}. "
        f"The bug in _convert_output_item_to_protocol converts all tool outputs to "
        f"function_call_result during serialization, causing them to be incorrectly "
        f"deserialized as FunctionCallOutput instead of LocalShellCallOutput."
    )

    deserialized_state = await RunStateClass.from_json(agent, json_data)

    # Verify that the shell output type is preserved after deserialization
    # When the bug exists, the item may be skipped due to validation errors
    # When fixed, it should deserialize correctly
    assert len(deserialized_state._generated_items) == 1, (
        "Shell output item should be deserialized. When the bug exists, it may be skipped "
        "due to validation errors when trying to deserialize as FunctionCallOutput instead "
        "of LocalShellCallOutput."
    )
    deserialized_item = deserialized_state._generated_items[0]
    assert isinstance(deserialized_item, ToolCallOutputItem)

    raw_item = deserialized_item.raw_item
    if isinstance(raw_item, dict):
        output_type = raw_item.get("type")
        assert output_type == "local_shell_call_output", (
            f"Expected local_shell_call_output, but got {output_type}. "
            f"The bug converts all tool outputs to function_call_result during serialization, "
            f"causing them to be incorrectly deserialized as FunctionCallOutput."
        )
    else:
        assert hasattr(raw_item, "type")
        assert raw_item.type == "local_shell_call_output", (
            f"Expected local_shell_call_output, but got {raw_item.type}. "
            f"The bug converts all tool outputs to function_call_result during serialization, "
            f"causing them to be incorrectly deserialized as FunctionCallOutput."
        )


@pytest.mark.asyncio
async def test_approvals_checked_before_executing_other_tools():
    """Test that approvals are checked before executing other tools.

    When a function tool requires approval and a shell tool doesn't, the shell tool
    should not execute before the function tool approval is checked. This ensures
    that side effects from tools that don't require approval don't occur before
    tools that require approval are properly handled.

    This test verifies that when a function tool requires approval, other tools
    (like shell tools) do not execute before the approval check.
    """
    model = FakeModel()
    shell_executed = False

    # Create a function tool that requires approval
    async def function_tool_func() -> str:
        return "function_result"

    async def needs_approval(_ctx, _params, _call_id) -> bool:
        return True

    function_tool_with_approval = function_tool(
        function_tool_func, name_override="function_tool", needs_approval=needs_approval
    )

    # Create a shell tool that doesn't require approval
    # Track execution to verify it doesn't run before approval check
    async def shell_executor(request: Any) -> str:
        nonlocal shell_executed
        shell_executed = True
        return "shell_output"

    shell_tool = ShellTool(executor=shell_executor)

    agent = Agent(name="TestAgent", model=model, tools=[function_tool_with_approval, shell_tool])

    # Create tool calls for both tools
    function_tool_call = get_function_tool_call("function_tool", "{}", call_id="call_func_1")
    assert isinstance(function_tool_call, ResponseFunctionToolCall)
    shell_tool_call = cast(
        Any,
        {
            "type": "shell_call",
            "id": "shell_1",
            "call_id": "call_shell_1",
            "status": "in_progress",
            "action": {"type": "exec", "commands": ["echo test"], "timeout_ms": 1000},
        },
    )

    # Create ProcessedResponse with both tools
    function_run = ToolRunFunction(
        function_tool=function_tool_with_approval, tool_call=function_tool_call
    )
    shell_run = ToolRunShellCall(tool_call=shell_tool_call, shell_tool=shell_tool)

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[function_run],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[shell_run],
        apply_patch_calls=[],
        mcp_approval_requests=[],
        tools_used=[],
        interruptions=[],
    )

    # Execute tools - should check approvals before executing shell tool
    result = await RunImpl.execute_tools_and_side_effects(
        agent=agent,
        original_input="test",
        pre_step_items=[],
        new_response=None,  # type: ignore[arg-type]
        processed_response=processed_response,
        output_schema=None,
        hooks=RunHooks(),
        context_wrapper=RunContextWrapper(context={}),
        run_config=RunConfig(),
    )

    # Should have interruptions since function tool needs approval
    assert isinstance(result.next_step, NextStepInterruption)
    assert len(result.next_step.interruptions) == 1
    assert isinstance(result.next_step.interruptions[0], ToolApprovalItem)

    # The shell tool should NOT have been executed because the function tool
    # requires approval and approvals should be checked before executing other tools.
    # This ensures that side effects from tools that don't require approval don't
    # occur before tools that require approval are properly handled.
    assert not shell_executed, (
        f"Expected shell tool to NOT execute when function tool requires approval, "
        f"but shell_executed is {shell_executed}. "
        f"Approvals should be checked before executing other tools to prevent "
        f"side effects from occurring before approval."
    )


@pytest.mark.asyncio
async def test_resuming_with_unapproved_tool_raises_interruption():
    """Test that resuming with an unapproved tool raises an interruption.

    When a tool requires approval and the run is resumed without approving it,
    the tool execution should return a ToolApprovalItem and an interruption should
    be raised. The resolve_interrupted_turn method should check tool results for
    ToolApprovalItem and return NextStepInterruption if found.

    This test verifies that when resuming with an unapproved tool, an interruption
    is raised rather than silently continuing.
    """
    model = FakeModel()

    async def function_tool_func() -> str:
        return "function_result"

    async def needs_approval(_ctx, _params, _call_id) -> bool:
        return True

    function_tool_with_approval = function_tool(
        function_tool_func, name_override="function_tool", needs_approval=needs_approval
    )

    agent = Agent(name="TestAgent", model=model, tools=[function_tool_with_approval])

    # First turn: tool call requiring approval
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("function_tool", "{}", call_id="call_func_1")],
            [get_text_message("done")],
        ]
    )

    result1 = await Runner.run(agent, "Use function_tool")
    assert result1.interruptions, "should have an interruption for function tool approval"
    assert len(result1.interruptions) == 1
    assert isinstance(result1.interruptions[0], ToolApprovalItem)

    # Create state but DO NOT approve the tool
    state = result1.to_state()

    # Set up next model response (final output)
    model.set_next_output([get_text_message("done")])

    # Resume from state without approving - should raise interruption
    # because the tool still needs approval
    result2 = await Runner.run(agent, state)

    # Should have interruptions since the tool was not approved
    # The resolve_interrupted_turn method should check tool results for
    # ToolApprovalItem and return NextStepInterruption if found.
    assert result2.interruptions, (
        f"Expected interruption when resuming with unapproved tool, "
        f"but got {len(result2.interruptions)} interruptions. "
        f"When a tool requires approval and is not approved, resolve_interrupted_turn "
        f"should check tool results for ToolApprovalItem and return NextStepInterruption."
    )
    assert len(result2.interruptions) == 1
    assert isinstance(result2.interruptions[0], ToolApprovalItem)


@pytest.mark.asyncio
async def test_resuming_after_approval_only_executes_unapproved_tools():
    """Test that resuming after approval only executes tools that weren't already run.

    When a turn has multiple tools where some require approval and some don't,
    the tools that don't require approval execute immediately. When resuming
    after approving the tools that require approval, only the approved tools
    should execute. Tools that were already executed should not be executed again.

    The resolve_interrupted_turn method should check original_pre_step_items for
    tool outputs and skip tools that were already executed to avoid duplicate
    side effects.

    This test verifies that already-executed tools are not re-executed when
    resuming after approval.
    """
    model = FakeModel()

    # Track execution counts for each tool
    tool_a_execution_count = 0
    tool_b_execution_count = 0

    # Tool A: doesn't require approval, executes immediately
    async def tool_a_func() -> str:
        nonlocal tool_a_execution_count
        tool_a_execution_count += 1
        return "tool_a_result"

    tool_a = function_tool(tool_a_func, name_override="tool_a", needs_approval=False)

    # Tool B: requires approval
    async def tool_b_func() -> str:
        nonlocal tool_b_execution_count
        tool_b_execution_count += 1
        return "tool_b_result"

    async def needs_approval(_ctx: Any, _params: Any, _call_id: str) -> bool:
        # Only tool_b needs approval
        return _call_id == "call_b_1"

    tool_b = function_tool(tool_b_func, name_override="tool_b", needs_approval=needs_approval)

    agent = Agent(name="TestAgent", model=model, tools=[tool_a, tool_b])

    # Single turn: both tools are called
    # Tool A executes immediately (no approval needed)
    # Tool B needs approval, causing an interruption
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("tool_a", "{}", call_id="call_a_1"),
                get_function_tool_call("tool_b", "{}", call_id="call_b_1"),
            ],
            [get_text_message("done")],
        ]
    )

    result1 = await Runner.run(agent, "Use both tools")
    assert result1.interruptions, "should have an interruption for tool_b approval"
    assert len(result1.interruptions) == 1
    assert isinstance(result1.interruptions[0], ToolApprovalItem)

    # Tool A should have executed once (before the interruption)
    assert tool_a_execution_count == 1, (
        f"Expected tool_a to execute once before interruption, "
        f"but got {tool_a_execution_count} executions."
    )

    # Tool B should not have executed yet (needs approval)
    assert tool_b_execution_count == 0, (
        f"Expected tool_b to not execute before approval, "
        f"but got {tool_b_execution_count} executions."
    )

    # Approve tool_b and resume
    state = result1.to_state()
    state.approve(result1.interruptions[0])

    # Manually remove the ToolApprovalItem from generated_items to simulate
    # a scenario where function_call_ids can't be collected (e.g., deserialization issue).
    # This forces the fallback case where ALL function tools are executed.
    # In this fallback case, tool_a (which was already executed) should not be
    # executed again. The resolve_interrupted_turn method should check
    # original_pre_step_items for tool outputs and skip tools that were already executed.
    state._generated_items = [
        item for item in state._generated_items if not isinstance(item, ToolApprovalItem)
    ]

    # Set up next model response (final output)
    model.set_next_output([get_text_message("done")])

    # Resume from state after approval
    # The processed_response contains both tool_a and tool_b from the original turn.
    # When resolve_interrupted_turn executes tools, it tries to filter function tools
    # by approved call_ids. However, if function_call_ids can't be collected
    # (e.g., ToolApprovalItem was removed from generated_items), the fallback case
    # executes ALL function tools from processed_response.functions without checking
    # if they were already executed. Tool A was already executed, so it should not
    # be executed again.
    await Runner.run(agent, state)

    # Tool A should still have executed only once (not re-executed on resume)
    # The resolve_interrupted_turn method should check original_pre_step_items for
    # tool outputs and skip tools that were already executed to avoid duplicate
    # side effects.
    assert tool_a_execution_count == 1, (
        f"Expected tool_a to execute only once total (not re-executed on resume), "
        f"but got {tool_a_execution_count} executions. "
        f"Already-executed tools should not be re-executed when resuming after approval. "
        f"resolve_interrupted_turn should check original_pre_step_items for tool outputs "
        f"and skip tools that were already executed."
    )

    # Tool B should have executed once (after approval)
    assert tool_b_execution_count == 1, (
        f"Expected tool_b to execute once after approval, "
        f"but got {tool_b_execution_count} executions."
    )


@pytest.mark.asyncio
async def test_mcp_approval_providerdata_preserved_on_deserialization():
    """Test that providerData is preserved when deserializing MCP approval interruptions.

    When a run is interrupted with an MCP approval request and the state is serialized
    to JSON and then deserialized, the providerData field should be preserved in the
    raw_item. This is needed for resolve_interrupted_turn to identify MCP approval
    requests by checking providerData.type == "mcp_approval_request".

    The _normalize_field_names function should preserve providerData for MCP approvals
    so they can be properly identified when resuming from a deserialized state.
    """
    model = FakeModel()
    agent = Agent(name="TestAgent", model=model, tools=[])

    # Create a ToolApprovalItem with an MCP approval request
    # This simulates an interruption with an MCP tool that requires approval
    mcp_raw_item = {
        "type": "hosted_tool_call",
        "name": "test_mcp_tool",
        "id": "mcp_call_1",
        "call_id": "call_mcp_1",
        "providerData": {
            "type": "mcp_approval_request",
            "id": "req-1",
            "server_label": "test_server",
            "approval_url": "https://example.com/approve",
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

    # Deserialize from JSON
    deserialized_state = await RunStateClass.from_json(agent, state_json)

    # Verify that the interruption was correctly deserialized
    assert deserialized_state._current_step is not None
    assert isinstance(deserialized_state._current_step, NextStepInterruption)
    assert len(deserialized_state._current_step.interruptions) == 1

    deserialized_item = deserialized_state._current_step.interruptions[0]
    assert isinstance(deserialized_item, ToolApprovalItem)

    # Verify that providerData is preserved in the raw_item
    # This is critical for resolve_interrupted_turn to identify MCP approvals
    raw_item = deserialized_item.raw_item
    assert isinstance(raw_item, dict), (
        f"Expected raw_item to be a dict after deserialization, "
        f"but got {type(raw_item)}. providerData should be preserved "
        f"so MCP approvals can be identified when resuming."
    )

    # Check that providerData exists and has the correct type
    provider_data = raw_item.get("providerData") or raw_item.get("provider_data")
    assert provider_data is not None, (
        f"Expected providerData to be preserved in raw_item after deserialization, "
        f"but it was missing. providerData is needed to identify MCP approval requests "
        f"when resuming from a deserialized state. Raw item keys: {list(raw_item.keys())}"
    )

    assert isinstance(provider_data, dict), (
        f"Expected providerData to be a dict, but got {type(provider_data)}."
    )

    assert provider_data.get("type") == "mcp_approval_request", (
        f"Expected providerData.type to be 'mcp_approval_request', "
        f"but got {provider_data.get('type')}. This is needed for "
        f"resolve_interrupted_turn to identify MCP approval requests."
    )
