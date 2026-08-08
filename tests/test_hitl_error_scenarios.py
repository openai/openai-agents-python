"""Regression tests for HITL edge cases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Optional, cast

import pytest
from openai.types.responses import (
    ResponseComputerToolCall,
    ResponseCustomToolCall,
    ResponseFunctionToolCall,
)
from openai.types.responses.response_computer_tool_call import ActionScreenshot
from openai.types.responses.response_input_param import (
    ComputerCallOutput,
    LocalShellCallOutput,
)
from openai.types.responses.response_output_item import LocalShellCall, McpApprovalRequest

from agents import (
    Agent,
    AgentBase,
    ApplyPatchTool,
    ComputerTool,
    CustomTool,
    LocalShellTool,
    Runner,
    RunResult,
    RunState,
    ShellTool,
    ToolApprovalItem,
    ToolExecutionConfig,
    function_tool,
    handoff,
    tool_namespace,
)
from agents._public_agent import set_public_agent
from agents.computer import Computer, Environment
from agents.exceptions import ModelBehaviorError, UserError
from agents.items import (
    MCPApprovalResponseItem,
    MessageOutputItem,
    ModelResponse,
    RunItem,
    ToolCallOutputItem,
    TResponseOutputItem,
)
from agents.lifecycle import RunHooks
from agents.run import RunConfig
from agents.run_context import RunContextWrapper
from agents.run_internal import run_loop
from agents.run_internal.agent_bindings import bind_execution_agent, bind_public_agent
from agents.run_internal.run_loop import (
    NextStepInterruption,
    NextStepRunAgain,
    ProcessedResponse,
    ToolRunApplyPatchCall,
    ToolRunComputerAction,
    ToolRunFunction,
    ToolRunHandoff,
    ToolRunMCPApprovalRequest,
    ToolRunShellCall,
    extract_tool_call_id,
)
from agents.run_internal.run_steps import ToolRunCustom
from agents.run_internal.tool_actions import ApplyPatchAction, CustomToolAction, ShellAction
from agents.run_internal.tool_execution import execute_function_tool_calls
from agents.run_internal.tool_planning import (
    _collect_runs_by_approval,
    _select_function_tool_runs_for_resume,
    execute_mcp_approval_requests,
)
from agents.run_state import RunState as RunStateClass
from agents.tool import FunctionTool, HostedMCPTool
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.usage import Usage

from .fake_model import FakeModel
from .mcp.helpers import FakeMCPServer
from .test_responses import get_text_message
from .utils.hitl import (
    HITL_REJECTION_MSG,
    ApprovalScenario,
    PendingScenario,
    RecordingEditor,
    approve_first_interruption,
    assert_pending_resume,
    assert_roundtrip_tool_name,
    assert_tool_output_roundtrip,
    collect_tool_outputs,
    consume_stream,
    make_agent,
    make_apply_patch_dict,
    make_context_wrapper,
    make_function_tool_call,
    make_mcp_approval_item,
    make_model_and_agent,
    make_shell_call,
    make_state_with_interruptions,
    queue_function_call_and_text,
    require_approval,
    resume_after_first_approval,
    run_and_resume_after_approval,
)


def _bind_agent(agent: Agent[Any]):
    public_agent = getattr(agent, "_agents_public_agent", None)
    if isinstance(public_agent, Agent):
        return bind_execution_agent(public_agent=public_agent, execution_agent=agent)
    return bind_public_agent(agent)


async def _resolve_interrupted_turn(*, agent: Agent[Any], **kwargs: Any):
    return await run_loop.resolve_interrupted_turn(
        bindings=_bind_agent(agent),
        **kwargs,
    )


class TrackingComputer(Computer):
    """Minimal computer implementation that records method calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def environment(self) -> Environment:
        return "mac"

    @property
    def dimensions(self) -> tuple[int, int]:
        return (1, 1)

    def screenshot(self) -> str:
        self.calls.append("screenshot")
        return "img"

    def click(self, _x: int, _y: int, _button: str) -> None:
        self.calls.append("click")

    def double_click(self, _x: int, _y: int) -> None:
        self.calls.append("double_click")

    def scroll(self, _x: int, _y: int, _scroll_x: int, _scroll_y: int) -> None:
        self.calls.append("scroll")

    def type(self, _text: str) -> None:
        self.calls.append("type")

    def wait(self) -> None:
        self.calls.append("wait")

    def move(self, _x: int, _y: int) -> None:
        self.calls.append("move")

    def keypress(self, _keys: list[str]) -> None:
        self.calls.append("keypress")

    def drag(self, _path: list[tuple[int, int]]) -> None:
        self.calls.append("drag")


def _shell_approval_setup() -> ApprovalScenario:
    tool = ShellTool(executor=lambda request: "shell_output", needs_approval=require_approval)
    shell_call = make_shell_call("call_shell_1", id_value="shell_1", commands=["echo test"])

    def _assert(result: RunResult) -> None:
        shell_outputs = collect_tool_outputs(result.new_items, output_type="shell_call_output")
        assert shell_outputs, "Shell tool should have been executed after approval"
        assert any("shell_output" in str(item.output) for item in shell_outputs)

    return ApprovalScenario(
        tool=tool,
        raw_call=shell_call,
        final_output=get_text_message("done"),
        assert_result=_assert,
    )


def _apply_patch_approval_setup() -> ApprovalScenario:
    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=editor, needs_approval=require_approval)
    apply_patch_call = make_apply_patch_dict("call_apply_1")

    def _assert(result: RunResult) -> None:
        apply_patch_outputs = collect_tool_outputs(
            result.new_items, output_type="apply_patch_call_output"
        )
        assert apply_patch_outputs, "ApplyPatch tool should have been executed after approval"
        assert editor.operations, "Editor should have been called"

    return ApprovalScenario(
        tool=tool,
        raw_call=apply_patch_call,
        final_output=get_text_message("done"),
        assert_result=_assert,
    )


def _shell_pending_setup() -> PendingScenario:
    tool = ShellTool(executor=lambda _req: "shell_output", needs_approval=True)
    raw_call = make_shell_call(
        "call_shell_pending", id_value="shell_pending", commands=["echo pending"]
    )
    return PendingScenario(tool=tool, raw_call=raw_call)


def _apply_patch_pending_setup() -> PendingScenario:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor, needs_approval=True)

    def _assert_editor(_resumed: RunResult) -> None:
        assert editor.operations == [], "editor should not run before approval"

    return PendingScenario(
        tool=apply_patch_tool,
        raw_call=make_apply_patch_dict("call_apply_pending"),
        assert_result=_assert_editor,
    )


@pytest.mark.parametrize(
    "setup_fn, user_input",
    [
        (_shell_approval_setup, "run shell command"),
        (_apply_patch_approval_setup, "update file"),
    ],
    ids=["shell_approved", "apply_patch_approved"],
)
@pytest.mark.asyncio
async def test_resumed_hitl_executes_approved_tools(
    setup_fn: Callable[[], ApprovalScenario],
    user_input: str,
) -> None:
    """Approved tools should run once the interrupted turn resumes."""
    scenario = setup_fn()
    model, agent = make_model_and_agent(tools=[scenario.tool])

    result = await run_and_resume_after_approval(
        agent,
        model,
        scenario.raw_call,
        scenario.final_output,
        user_input=user_input,
    )

    scenario.assert_result(result)


@pytest.mark.parametrize(
    "tool_kind", ["shell", "apply_patch"], ids=["shell_auto", "apply_patch_auto"]
)
@pytest.mark.asyncio
async def test_resuming_skips_approvals_for_non_hitl_tools(tool_kind: str) -> None:
    """Auto-approved tools should not trigger new approvals when resuming a turn."""
    shell_runs: list[str] = []
    editor: RecordingEditor | None = None
    auto_tool: ShellTool | ApplyPatchTool

    if tool_kind == "shell":

        def _executor(_req: Any) -> str:
            shell_runs.append("run")
            return "shell_output"

        auto_tool = ShellTool(executor=_executor)
        raw_call = make_shell_call("call_shell_auto", id_value="shell_auto", commands=["echo auto"])
        output_type = "shell_call_output"
    else:
        editor = RecordingEditor()
        auto_tool = ApplyPatchTool(editor=editor)
        raw_call = make_apply_patch_dict("call_apply_auto")
        output_type = "apply_patch_call_output"

    async def needs_hitl() -> str:
        return "approved"

    approval_tool = function_tool(needs_hitl, needs_approval=require_approval)
    model, agent = make_model_and_agent(tools=[auto_tool, approval_tool])

    function_call = make_function_tool_call(approval_tool.name, call_id="call-func-auto")

    queue_function_call_and_text(
        model,
        function_call,
        first_turn_extra=[raw_call],
        followup=[get_text_message("done")],
    )

    first = await Runner.run(agent, "resume approvals")
    assert first.interruptions, "function tool should require approval"

    resumed = await resume_after_first_approval(agent, first, always_approve=True)

    assert not resumed.interruptions, "non-HITL tools should not request approval on resume"

    outputs = collect_tool_outputs(resumed.new_items, output_type=output_type)
    assert len(outputs) == 1, f"{tool_kind} should run exactly once without extra approvals"

    if tool_kind == "shell":
        assert len(shell_runs) == 1, "shell should execute automatically when resuming"
    else:
        assert editor is not None
        assert len(editor.operations) == 1, "apply_patch should execute once when resuming"


@pytest.mark.asyncio
async def test_nested_agent_tool_resumes_after_rejection() -> None:
    """A nested agent tool should resume after a rejection to continue its own flow."""

    @function_tool(needs_approval=True)
    async def inner_hitl_tool() -> str:
        return "ok"

    inner_model = FakeModel()
    inner_agent = Agent(name="Inner", model=inner_model, tools=[inner_hitl_tool])
    inner_call_first = make_function_tool_call(inner_hitl_tool.name, call_id="inner-1")
    inner_call_retry = make_function_tool_call(inner_hitl_tool.name, call_id="inner-2")
    inner_final = get_text_message("done")
    inner_model.add_multiple_turn_outputs(
        [
            [inner_call_first],
            [inner_call_retry],
            [inner_final],
        ]
    )

    agent_tool = inner_agent.as_tool(
        tool_name="inner_agent_tool",
        tool_description="Inner agent tool with HITL",
        needs_approval=True,
    )

    outer_model = FakeModel()
    outer_agent = Agent(name="Outer", model=outer_model, tools=[agent_tool])
    outer_call = make_function_tool_call(
        agent_tool.name, call_id="outer-1", arguments='{"input":"hi"}'
    )
    outer_model.add_multiple_turn_outputs([[outer_call]])

    first = await Runner.run(outer_agent, "start")
    assert first.interruptions, "agent tool should request approval first"
    assert first.interruptions[0].tool_name == agent_tool.name

    state_after_outer_approval = first.to_state()
    state_after_outer_approval.approve(first.interruptions[0], always_approve=True)

    second = await Runner.run(outer_agent, state_after_outer_approval)
    assert second.interruptions, "inner tool should request approval on first run"
    assert second.interruptions[0].tool_name == inner_hitl_tool.name

    state_after_inner_reject = second.to_state()
    state_after_inner_reject.reject(second.interruptions[0])

    third = await Runner.run(outer_agent, state_after_inner_reject)
    assert third.interruptions, "nested agent should resume and request new approval"
    assert third.interruptions[0].tool_name == inner_hitl_tool.name
    assert extract_tool_call_id(third.interruptions[0].raw_item) == "inner-2"
    rejection_outputs = [
        item
        for item in third.new_items
        if isinstance(item, ToolCallOutputItem)
        and item.output == HITL_REJECTION_MSG
        and extract_tool_call_id(item.raw_item) == "outer-1"
    ]
    assert not rejection_outputs, "Nested rejection should not short-circuit the agent tool"


@pytest.mark.asyncio
async def test_changed_nested_parent_fails_before_tool_inventory_callbacks() -> None:
    enabled_calls: list[str] = []

    async def enabled(_context: RunContextWrapper[Any], agent: AgentBase[Any]) -> bool:
        enabled_calls.append(agent.name)
        return True

    @function_tool(needs_approval=True)
    async def inner_hitl_tool() -> str:
        return "ok"

    @function_tool(is_enabled=enabled)
    async def observer() -> str:
        return "unused"

    inner_model = FakeModel()
    inner_model.add_multiple_turn_outputs(
        [[make_function_tool_call(inner_hitl_tool.name, call_id="inner-1")]]
    )
    inner_agent = Agent(name="Inner", model=inner_model, tools=[inner_hitl_tool])
    agent_tool = inner_agent.as_tool(
        tool_name="inner_agent_tool",
        tool_description="Inner agent tool with HITL",
        needs_approval=True,
    )
    outer_model = FakeModel(
        initial_output=[
            make_function_tool_call(
                agent_tool.name,
                call_id="outer-1",
                arguments='{"input":"safe"}',
            )
        ]
    )
    outer_agent = Agent(name="Outer", model=outer_model, tools=[agent_tool, observer])

    first = await Runner.run(outer_agent, "start")
    first_state = first.to_state()
    first_state.approve(first.interruptions[0])
    second = await Runner.run(outer_agent, first_state)
    assert second.interruptions[0].tool_name == inner_hitl_tool.name

    resume_state = second.to_state()
    assert resume_state._last_processed_response is not None
    enabled_calls.clear()
    resume_state._last_processed_response.functions[0].tool_call.arguments = '{"input":"evil"}'
    resume_state.approve(second.interruptions[0])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(outer_agent, resume_state)

    assert enabled_calls == []


@pytest.mark.asyncio
async def test_nested_agent_tool_interruptions_remain_distinct_across_outer_calls() -> None:
    """Nested agent tool interruptions should survive multiple outer calls."""

    @function_tool(needs_approval=True)
    async def inner_hitl_tool() -> str:
        return "ok"

    inner_model = FakeModel()
    inner_agent = Agent(name="Inner", model=inner_model, tools=[inner_hitl_tool])
    inner_model.add_multiple_turn_outputs(
        [
            [make_function_tool_call(inner_hitl_tool.name, call_id="inner-1")],
            [make_function_tool_call(inner_hitl_tool.name, call_id="inner-2")],
        ]
    )

    agent_tool = inner_agent.as_tool(
        tool_name="inner_agent_tool",
        tool_description="Inner agent tool",
        needs_approval=False,
    )

    outer_model = FakeModel()
    outer_agent = Agent(name="Outer", model=outer_model, tools=[agent_tool])
    outer_model.add_multiple_turn_outputs(
        [
            [
                make_function_tool_call(
                    agent_tool.name, call_id="outer-a", arguments='{"input":"a"}'
                ),
                make_function_tool_call(
                    agent_tool.name, call_id="outer-b", arguments='{"input":"b"}'
                ),
            ]
        ]
    )

    result = await Runner.run(outer_agent, "start")
    assert result.interruptions, "nested agent tool should request approvals"
    nested_interruptions = [
        item for item in result.interruptions if item.tool_name == inner_hitl_tool.name
    ]
    assert len(nested_interruptions) == 2


@pytest.mark.asyncio
async def test_nested_agent_tool_does_not_inherit_parent_approvals() -> None:
    """Nested agent tools should request approval even if parent approved the same call ID."""

    @function_tool(needs_approval=True, name_override="shared_tool")
    async def outer_shared_tool() -> str:
        return "outer"

    @function_tool(needs_approval=True, name_override="shared_tool")
    async def inner_shared_tool() -> str:
        return "inner"

    inner_model = FakeModel()
    inner_agent = Agent(name="Inner", model=inner_model, tools=[inner_shared_tool])
    inner_model.add_multiple_turn_outputs(
        [[make_function_tool_call(inner_shared_tool.name, call_id="dup")]]
    )

    agent_tool = inner_agent.as_tool(
        tool_name="inner_agent_tool",
        tool_description="Inner agent tool",
        needs_approval=False,
    )

    outer_model = FakeModel()
    outer_agent = Agent(name="Outer", model=outer_model, tools=[outer_shared_tool, agent_tool])
    outer_model.add_multiple_turn_outputs(
        [
            [make_function_tool_call(outer_shared_tool.name, call_id="dup")],
            [
                make_function_tool_call(
                    agent_tool.name, call_id="outer-agent", arguments='{"input":"hi"}'
                )
            ],
        ]
    )

    first = await Runner.run(outer_agent, "start")
    assert first.interruptions, "parent tool should request approval first"

    approved_state = first.to_state()
    approved_state.approve(first.interruptions[0])

    second = await Runner.run(outer_agent, approved_state)
    assert second.interruptions, "nested tool should still require approval"
    assert any(item.tool_name == inner_shared_tool.name for item in second.interruptions), (
        "inner tool approvals should not inherit parent approvals"
    )


@pytest.mark.parametrize(
    "setup_fn, output_type",
    [
        (_shell_pending_setup, "shell_call_output"),
        (_apply_patch_pending_setup, "apply_patch_call_output"),
    ],
    ids=["shell_pending", "apply_patch_pending"],
)
@pytest.mark.asyncio
async def test_pending_approvals_stay_pending_on_resume(
    setup_fn: Callable[[], PendingScenario],
    output_type: str,
) -> None:
    """Unapproved tool calls should remain pending after resuming a run."""
    scenario = setup_fn()
    model, _ = make_model_and_agent()

    resumed = await assert_pending_resume(
        scenario.tool,
        model,
        scenario.raw_call,
        user_input="resume pending approval",
        output_type=output_type,
    )

    if scenario.assert_result:
        scenario.assert_result(resumed)


@pytest.mark.asyncio
async def test_resume_does_not_duplicate_pending_shell_approvals() -> None:
    """Resuming should not duplicate pending shell approvals."""
    tool = ShellTool(executor=lambda _request: "shell_output", needs_approval=True)
    model, agent = make_model_and_agent(tools=[tool])
    raw_call = make_shell_call(
        "call_shell_pending_dup",
        id_value="shell_pending_dup",
        commands=["echo pending"],
    )
    call_id = extract_tool_call_id(raw_call)
    assert call_id, "shell call must have a call_id"

    model.set_next_output([raw_call])
    first = await Runner.run(agent, "run shell")
    assert first.interruptions, "shell tool should require approval"

    resumed = await Runner.run(agent, first.to_state())
    pending_items = [
        item
        for item in resumed.new_items
        if isinstance(item, ToolApprovalItem) and extract_tool_call_id(item.raw_item) == call_id
    ]
    assert len(pending_items) == 1


@pytest.mark.asyncio
async def test_resuming_pending_mcp_approvals_raises_typeerror():
    """ToolApprovalItem must be hashable so pending MCP approvals can be tracked in a set."""
    _, agent = make_model_and_agent(tools=[])

    mcp_approval_item = make_mcp_approval_item(
        agent, call_id="mcp-approval-1", include_provider_data=False
    )

    pending_hosted_mcp_approvals: set[ToolApprovalItem] = set()
    pending_hosted_mcp_approvals.add(mcp_approval_item)
    assert mcp_approval_item in pending_hosted_mcp_approvals


@pytest.mark.asyncio
async def test_route_local_shell_calls_to_remote_shell_tool():
    """Test that local shell calls are routed to the local shell tool.

    When processing model output with LocalShellCall items, they should be handled by
    LocalShellTool (not ShellTool), even when both tools are registered. This ensures
    local shell operations use the correct executor and approval hooks.
    """
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
    model, agent = make_model_and_agent(tools=[shell_tool, local_shell_tool])

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

    A run configured with max_turns=20 should keep that limit after resuming from
    result.to_state() without re-passing max_turns.
    """

    async def test_tool() -> str:
        return "tool_result"

    # Create the tool with needs_approval directly
    # The tool name will be "test_tool" based on the function name
    tool = function_tool(test_tool, needs_approval=require_approval)
    model, agent = make_model_and_agent(tools=[tool])

    model.add_multiple_turn_outputs([[make_function_tool_call("test_tool", call_id="call-1")]])

    result1 = await Runner.run(agent, "call test_tool", max_turns=20)
    assert result1.interruptions, "should have an interruption"

    state = approve_first_interruption(result1, always_approve=True)

    # Provide 10 more turns (turns 2-11) to ensure we exceed the default 10 but not 20.
    model.add_multiple_turn_outputs(
        [
            [
                get_text_message(f"turn {i + 2}"),  # Text message first (doesn't finish)
                make_function_tool_call("test_tool", call_id=f"call-{i + 2}"),
            ]
            for i in range(10)
        ]
    )

    result2 = await Runner.run(agent, state)
    assert result2 is not None, "Run should complete successfully with max_turns=20 from state"


@pytest.mark.asyncio
async def test_current_turn_not_preserved_in_to_state():
    """Test that current turn counter is preserved when converting RunResult to RunState."""

    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, needs_approval=require_approval)
    model, agent = make_model_and_agent(tools=[tool])

    # Model emits a tool call requiring approval
    model.set_next_output([make_function_tool_call("test_tool", call_id="call-1")])

    # First turn with interruption
    result1 = await Runner.run(agent, "call test_tool")
    assert result1.interruptions, "should have interruption on turn 1"

    # Convert to state - this should preserve current_turn=1
    state1 = result1.to_state()

    # Regression guard: to_state should keep the turn counter instead of resetting it.
    assert state1._current_turn == 1, (
        f"Expected current_turn=1 after 1 turn, got {state1._current_turn}. "
        "to_state() should preserve the current turn counter."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_factory, raw_call_factory, expected_tool_name, user_input",
    [
        (
            lambda: ShellTool(executor=lambda request: "output", needs_approval=require_approval),
            lambda: make_shell_call("call_shell_1", id_value="shell_1", commands=["echo test"]),
            "shell",
            "run shell",
        ),
        (
            lambda: ApplyPatchTool(editor=RecordingEditor(), needs_approval=require_approval),
            lambda: cast(Any, make_apply_patch_dict("call_apply_1")),
            "apply_patch",
            "update file",
        ),
    ],
    ids=["shell", "apply_patch"],
)
@pytest.mark.asyncio
async def test_deserialize_interruptions_preserve_tool_calls(
    tool_factory: Callable[[], Any],
    raw_call_factory: Callable[[], TResponseOutputItem],
    expected_tool_name: str,
    user_input: str,
) -> None:
    """Ensure deserialized interruptions preserve tool types instead of forcing function calls."""
    model, agent = make_model_and_agent(tools=[tool_factory()])
    await assert_roundtrip_tool_name(
        agent, model, raw_call_factory(), expected_tool_name, user_input=user_input
    )


@pytest.mark.parametrize("include_provider_data", [True, False])
@pytest.mark.asyncio
async def test_deserialize_interruptions_preserve_mcp_tools(
    include_provider_data: bool,
) -> None:
    """Ensure MCP/hosted tool approvals survive serialization."""
    model, agent = make_model_and_agent(tools=[])

    mcp_approval_item = make_mcp_approval_item(
        agent, call_id="mcp-approval-1", include_provider_data=include_provider_data
    )
    state = make_state_with_interruptions(agent, [mcp_approval_item])

    state_json = state.to_json()

    deserialized_state = await RunStateClass.from_json(agent, state_json)
    interruptions = deserialized_state.get_interruptions()
    assert len(interruptions) > 0, "Interruptions should be preserved after deserialization"
    assert interruptions[0].tool_name == "test_mcp_tool", (
        "MCP tool approval should be preserved, not converted to function"
    )


@pytest.mark.asyncio
async def test_hosted_mcp_approval_with_unknown_legacy_identity_requires_reapproval() -> None:
    """Incomplete legacy MCP approvals cannot receive an authorization decision."""
    agent = make_agent()
    context_wrapper = make_context_wrapper()

    approval_item = make_mcp_approval_item(
        agent,
        call_id="mcp-123",
        provider_data={"type": "mcp_approval_request"},
        tool_name=None,
        include_name=False,
        use_call_id=False,
    )
    with pytest.raises(ModelBehaviorError, match="canonical invocation identity"):
        context_wrapper.approve_tool(approval_item)


@pytest.mark.asyncio
async def test_shell_call_without_call_id_raises() -> None:
    """Shell calls missing call_id should raise ModelBehaviorError instead of being skipped."""
    agent = make_agent()
    context_wrapper = make_context_wrapper()
    shell_tool = ShellTool(executor=lambda _request: "")
    shell_call = {"type": "shell_call", "action": {"commands": ["echo", "hi"]}}

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[ToolRunShellCall(tool_call=shell_call, shell_tool=shell_tool)],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    with pytest.raises(ModelBehaviorError):
        await _resolve_interrupted_turn(
            agent=agent,
            original_input="test",
            original_pre_step_items=[],
            new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
            processed_response=processed_response,
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            run_config=RunConfig(),
            run_state=None,
        )


@pytest.mark.asyncio
async def test_preserve_persisted_item_counter_when_resuming_streamed_runs():
    """Preserve the persisted-item counter on streamed resume to avoid losing history."""
    model, agent = make_model_and_agent()

    # Simulate a turn interrupted mid-persistence: 5 items generated, 3 actually saved.
    context_wrapper = make_context_wrapper()
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

    # Persisted count reflects what was already written before interruption.
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

    result = Runner.run_streamed(agent, state)

    assert result._current_turn_persisted_item_count == 3, (
        f"Expected _current_turn_persisted_item_count=3 (the actual persisted count), "
        f"but got {result._current_turn_persisted_item_count}. "
        f"The counter should reflect persisted items, not len(_generated_items)="
        f"{len(state._generated_items)}."
    )

    await consume_stream(result)


@pytest.mark.asyncio
async def test_preserve_tool_output_types_during_serialization():
    """Keep tool output types intact during RunState serialization/deserialization."""

    model, agent = make_model_and_agent(tools=[])

    computer_output: ComputerCallOutput = {
        "type": "computer_call_output",
        "call_id": "call_computer_1",
        "output": {"type": "computer_screenshot", "image_url": "base64_screenshot_data"},
    }
    await assert_tool_output_roundtrip(
        agent, computer_output, "computer_call_output", output="screenshot_data"
    )

    # TypedDict requires "id", but runtime objects use "call_id"; cast to align with runtime shape.
    shell_output = cast(
        LocalShellCallOutput,
        {
            "type": "local_shell_call_output",
            "id": "shell_1",
            "call_id": "call_shell_1",
            "output": "command output",
        },
    )
    await assert_tool_output_roundtrip(agent, shell_output, "local_shell_call_output")


@pytest.mark.asyncio
async def test_function_needs_approval_invalid_type_raises() -> None:
    """needs_approval must be bool or callable; invalid types should raise UserError."""

    @function_tool(name_override="bad_tool", needs_approval=cast(Any, "always"))
    def bad_tool() -> str:
        return "ok"

    model, agent = make_model_and_agent(tools=[bad_tool])
    model.set_next_output([make_function_tool_call("bad_tool")])

    with pytest.raises(UserError, match="needs_approval"):
        await Runner.run(agent, "run invalid")


@pytest.mark.parametrize(
    "arguments",
    [
        '{"subject": "refund"',
        "null",
        "[]",
        '{"amount": NaN}',
        '{"amount": Infinity}',
        '{"amount": -Infinity}',
    ],
)
@pytest.mark.asyncio
async def test_callable_function_approval_fails_closed_for_invalid_arguments(
    arguments: str,
) -> None:
    """Uninspectable function arguments must require approval before a manual tool can run."""
    approval_inputs: list[dict[str, Any]] = []
    tool_inputs: list[str] = []

    async def needs_approval(_ctx: Any, params: dict[str, Any], _call_id: str) -> bool:
        approval_inputs.append(params)
        return False

    async def invoke_tool(_ctx: Any, raw_arguments: str) -> str:
        tool_inputs.append(raw_arguments)
        return "sent"

    tool = FunctionTool(
        name="send_email",
        description="Send an email.",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=invoke_tool,
        needs_approval=needs_approval,
    )
    model, agent = make_model_and_agent(tools=[tool])
    model.set_next_output(
        [make_function_tool_call(tool.name, arguments=arguments, call_id="call-invalid")]
    )

    result = await Runner.run(agent, "send an email")

    assert len(result.interruptions) == 1
    assert result.interruptions[0].tool_name == tool.name
    assert approval_inputs == []
    assert tool_inputs == []


@pytest.mark.asyncio
async def test_callable_function_approval_receives_valid_object_arguments() -> None:
    """Valid object arguments should preserve callable approval behavior."""
    approval_inputs: list[dict[str, Any]] = []
    tool_inputs: list[str] = []

    async def needs_approval(_ctx: Any, params: dict[str, Any], _call_id: str) -> bool:
        approval_inputs.append(params)
        return False

    async def invoke_tool(_ctx: Any, raw_arguments: str) -> str:
        tool_inputs.append(raw_arguments)
        return "sent"

    tool = FunctionTool(
        name="send_email",
        description="Send an email.",
        params_json_schema={"type": "object", "properties": {"subject": {"type": "string"}}},
        on_invoke_tool=invoke_tool,
        needs_approval=needs_approval,
    )
    arguments = '{"subject": "status update"}'
    model, agent = make_model_and_agent(tools=[tool])
    model.add_multiple_turn_outputs(
        [
            [make_function_tool_call(tool.name, arguments=arguments, call_id="call-valid")],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, "send an email")

    assert result.final_output == "done"
    assert approval_inputs
    assert all(params == {"subject": "status update"} for params in approval_inputs)
    assert tool_inputs == [arguments]


@pytest.mark.asyncio
async def test_resume_invalid_needs_approval_raises() -> None:
    """Resume path should surface invalid needs_approval configuration errors."""

    @function_tool(name_override="bad_tool", needs_approval=cast(Any, "always"))
    def bad_tool() -> str:
        return "ok"

    agent = make_agent(tools=[bad_tool])
    context_wrapper = make_context_wrapper()
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[
            ToolRunFunction(
                function_tool=bad_tool,
                tool_call=make_function_tool_call("bad_tool", call_id="call-1"),
            )
        ],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    with pytest.raises(UserError, match="needs_approval"):
        await _resolve_interrupted_turn(
            agent=agent,
            original_input="resume invalid",
            original_pre_step_items=[],
            new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
            processed_response=processed_response,
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            run_config=RunConfig(),
            run_state=None,
        )


@pytest.mark.asyncio
async def test_agent_as_tool_with_nested_approvals_propagates() -> None:
    """Agent-as-tool with needs_approval should still surface nested tool approvals."""

    nested_model, spanish_agent = make_model_and_agent(name="spanish_agent")
    tool_calls: list[str] = []

    @function_tool(needs_approval=True)
    async def get_current_timestamp() -> str:
        tool_calls.append("called")
        return "timestamp"

    spanish_agent.tools = [get_current_timestamp]

    # Spanish agent will first request timestamp, then return text.
    nested_model.add_multiple_turn_outputs(
        [
            [make_function_tool_call("get_current_timestamp")],
            [get_text_message("hola")],
        ]
    )

    # Orchestrator model will call the spanish agent tool.
    orchestrator_model = FakeModel()
    orchestrator = Agent(
        name="orchestrator",
        tools=[
            spanish_agent.as_tool(
                tool_name="respond_spanish",
                tool_description="Respond in Spanish",
                needs_approval=True,
            )
        ],
        model=orchestrator_model,
    )

    orchestrator_model.add_multiple_turn_outputs(
        [
            [
                make_function_tool_call(
                    "respond_spanish",
                    call_id="spanish-call",
                    arguments='{"input": "hola"}',
                )
            ],
            [get_text_message("done")],
        ]
    )

    # First run should surface approval for respond_spanish.
    first = await Runner.run(orchestrator, "hola")
    assert first.interruptions, "Outer agent tool should require approval"

    # Resuming should now surface nested approval from the Spanish agent.
    state = approve_first_interruption(first, always_approve=True)
    resumed = await Runner.run(orchestrator, state)
    assert resumed.interruptions, "Nested agent tool approval should bubble up"
    assert resumed.interruptions[0].tool_name == "get_current_timestamp"
    assert isinstance(resumed.to_input_list(), list)

    assert not tool_calls, "Nested tool should not execute before approval"

    final_state = approve_first_interruption(resumed, always_approve=True)
    final = await Runner.run(orchestrator, final_state)
    assert final.final_output == "done"
    assert tool_calls == ["called"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_nested_agent_tool_continuation_runs_outer_callbacks_once(streamed: bool) -> None:
    """Nested resume re-enters only the saved agent run, not the outer callback pipeline."""
    nested_model, nested_agent = make_model_and_agent(name="nested_agent")
    inner_calls: list[str] = []
    counts = {
        "input_guardrail": 0,
        "start": 0,
        "output_guardrail": 0,
        "custom_output": 0,
        "extractor": 0,
        "end": 0,
    }

    @function_tool(needs_approval=True)
    async def inner_tool() -> str:
        inner_calls.append("called")
        return "inner output"

    nested_agent.tools = [inner_tool]
    nested_model.add_multiple_turn_outputs(
        [
            [
                make_function_tool_call(
                    "inner_tool",
                    call_id=f"inner-call-{streamed}",
                )
            ],
            [get_text_message("nested done")],
        ]
    )

    @tool_input_guardrail
    def track_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        counts["input_guardrail"] += 1
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    def track_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        counts["output_guardrail"] += 1
        return ToolGuardrailFunctionOutput.allow()

    def extract_custom_data(_context: Any) -> dict[str, Any]:
        counts["extractor"] += 1
        return {"nested": True}

    async def extract_custom_output(result: Any) -> str:
        counts["custom_output"] += 1
        return cast(str, result.final_output)

    outer_tool = nested_agent.as_tool(
        tool_name="delegate",
        tool_description="Delegate to the nested agent",
        custom_output_extractor=extract_custom_output,
    )
    outer_tool.tool_input_guardrails = [track_input]
    outer_tool.tool_output_guardrails = [track_output]
    outer_tool.custom_data_extractor = extract_custom_data

    outer_model = FakeModel()
    outer_model.add_multiple_turn_outputs(
        [
            [
                make_function_tool_call(
                    "delegate",
                    call_id=f"outer-call-{streamed}",
                    arguments='{"input":"hello"}',
                )
            ],
            [get_text_message("outer done")],
        ]
    )
    outer_agent = Agent(name="outer_agent", model=outer_model, tools=[outer_tool])

    class CountingHooks(RunHooks[Any]):
        async def on_tool_start(
            self,
            _context: Any,
            _agent: Agent[Any],
            tool: Any,
        ) -> None:
            if tool.name == "delegate":
                counts["start"] += 1

        async def on_tool_end(
            self,
            _context: Any,
            _agent: Agent[Any],
            tool: Any,
            _result: object,
        ) -> None:
            if tool.name == "delegate":
                counts["end"] += 1

    hooks = CountingHooks()

    async def run(input_value: Any) -> Any:
        if not streamed:
            return await Runner.run(outer_agent, input_value, hooks=hooks)
        result = Runner.run_streamed(outer_agent, input_value, hooks=hooks)
        async for _event in result.stream_events():
            pass
        return result

    interrupted = await run("start")
    assert interrupted.interruptions
    assert counts["input_guardrail"] <= 1
    assert counts["start"] <= 1
    assert counts["output_guardrail"] == 0
    assert counts["custom_output"] == 0
    assert counts["extractor"] == 0
    assert counts["end"] == 0

    state = interrupted.to_state()
    state.approve(state.get_interruptions()[0])
    restored = await RunState.from_json(outer_agent, state.to_json())
    final = await run(restored)

    assert final.final_output == "outer done"
    assert inner_calls == ["called"]
    assert counts == {
        "input_guardrail": 1,
        "start": 1,
        "output_guardrail": 1,
        "custom_output": 1,
        "extractor": 1,
        "end": 1,
    }


@pytest.mark.asyncio
async def test_resume_rebuilds_function_runs_from_pending_approvals() -> None:
    """Resuming with only pending approvals should reconstruct and run function calls."""

    @function_tool(needs_approval=True)
    def approve_me(reason: Optional[str] = None) -> str:  # noqa: UP007
        return f"approved:{reason}" if reason else "approved"

    model, agent = make_model_and_agent(tools=[approve_me])
    approval_raw = {
        "type": "function_call",
        "name": approve_me.name,
        "call_id": "call-rebuild-1",
        "arguments": '{"reason": "ok"}',
        "status": "completed",
    }
    approval_item = ToolApprovalItem(agent=agent, raw_item=approval_raw)
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(approval_item)

    run_state = make_state_with_interruptions(agent, [approval_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert not isinstance(result.next_step, NextStepInterruption), (
        "Approved function should run instead of requesting approval again"
    )
    executed_call_ids = {
        extract_tool_call_id(item.raw_item)
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem)
    }
    assert "call-rebuild-1" in executed_call_ids, "Function should be rebuilt and executed"


@pytest.mark.asyncio
async def test_resume_rebuilds_deferred_function_runs_from_lookup_key_without_raw_namespace() -> (
    None
):
    """Resumed approvals should use persisted lookup identity when raw namespace is missing."""

    @function_tool(needs_approval=True, name_override="lookup_account")
    async def visible_lookup_account(customer_id: str) -> str:
        return f"visible:{customer_id}"

    @function_tool(
        needs_approval=True,
        name_override="lookup_account",
        defer_loading=True,
    )
    async def deferred_lookup_account(customer_id: str) -> str:
        return f"deferred:{customer_id}"

    _model, agent = make_model_and_agent(tools=[visible_lookup_account, deferred_lookup_account])
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "function_call",
            "name": "lookup_account",
            "call_id": "call-deferred-rebuild",
            "arguments": '{"customer_id":"customer_1"}',
            "status": "completed",
        },
        tool_name="lookup_account",
        tool_namespace="lookup_account",
        tool_lookup_key=("deferred_top_level", "lookup_account"),
    )
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(approval_item)

    run_state = make_state_with_interruptions(agent, [approval_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert not isinstance(result.next_step, NextStepInterruption)
    deferred_outputs = [
        item.output
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem) and item.output == "deferred:customer_1"
    ]
    assert deferred_outputs == ["deferred:customer_1"]


@pytest.mark.asyncio
async def test_resume_does_not_rebuild_approved_calls_for_same_named_sibling_agent() -> None:
    """Approved interruptions should match the current public agent, not any same-named sibling."""

    first_calls: list[str] = []
    second_calls: list[str] = []

    @function_tool(needs_approval=True, name_override="approval_tool")
    async def first_approval_tool() -> str:
        first_calls.append("first")
        return "first"

    @function_tool(needs_approval=True, name_override="approval_tool")
    async def second_approval_tool() -> str:
        second_calls.append("second")
        return "second"

    first = Agent(name="sandbox", tools=[first_approval_tool])
    second = Agent(name="sandbox", tools=[second_approval_tool])
    first.handoffs = [second]
    second.handoffs = [first]

    approval_item = ToolApprovalItem(
        agent=second,
        raw_item=make_function_tool_call(
            name="approval_tool",
            call_id="call-sibling-approval",
            arguments="{}",
        ),
        tool_name="approval_tool",
    )
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(approval_item)
    run_state = make_state_with_interruptions(first, [approval_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    execution_agent = set_public_agent(first.clone(), first)
    result = await _resolve_interrupted_turn(
        agent=execution_agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert first_calls == []
    assert second_calls == []
    assert not any(isinstance(item, ToolCallOutputItem) for item in result.new_step_items)


@pytest.mark.asyncio
async def test_resume_honors_permanent_namespaced_function_approval_with_new_call_id() -> None:
    @function_tool(needs_approval=True, name_override="lookup_account")
    async def lookup_account(customer_id: str) -> str:
        return customer_id

    namespaced_tool = tool_namespace(
        name="billing",
        description="Billing tools",
        tools=[lookup_account],
    )[0]
    context_wrapper = make_context_wrapper()
    approved_item = ToolApprovalItem(
        agent=Agent(name="billing-agent"),
        raw_item=make_function_tool_call(
            "lookup_account",
            call_id="approved-call",
            arguments='{"customer_id":"customer_1"}',
            namespace="billing",
        ),
    )
    context_wrapper.approve_tool(approved_item, always_approve=True)

    resumed_run = ToolRunFunction(
        tool_call=make_function_tool_call(
            "lookup_account",
            call_id="resumed-call",
            arguments='{"customer_id":"customer_2"}',
            namespace="billing",
        ),
        function_tool=namespaced_tool,
    )
    pending: list[ToolApprovalItem] = []
    rejections: list[str | None] = []

    async def _needs_approval_checker(_run: ToolRunFunction) -> bool:
        return True

    async def _record_rejection(
        call_id: str | None,
        _tool_call: ResponseFunctionToolCall,
        _tool: Any,
    ) -> None:
        rejections.append(call_id)

    selected = await _select_function_tool_runs_for_resume(
        [resumed_run],
        approval_items_by_call_id={},
        context_wrapper=context_wrapper,
        needs_approval_checker=_needs_approval_checker,
        output_exists_checker=lambda _run: False,
        record_rejection=_record_rejection,
        pending_interruption_adder=pending.append,
        pending_item_builder=lambda run: ToolApprovalItem(
            agent=Agent(name="billing-agent"),
            raw_item=run.tool_call,
            tool_name=run.function_tool.name,
            tool_namespace="billing",
        ),
    )

    assert selected == [resumed_run]
    assert pending == []
    assert rejections == []


@pytest.mark.asyncio
async def test_resume_skips_needs_approval_checker_when_status_resolved() -> None:
    """Resolved approve/reject decisions must short-circuit needs_approval_checker.

    A user-supplied checker may have side effects (telemetry, network, exceptions).
    When the approval status is already True or False, we must not invoke it.
    """

    @function_tool(needs_approval=True)
    async def approve_me(value: str) -> str:
        return value

    approved_call = make_function_tool_call(
        approve_me.name, call_id="approved-call", arguments='{"value":"a"}'
    )
    rejected_call = make_function_tool_call(
        approve_me.name, call_id="rejected-call", arguments='{"value":"b"}'
    )
    agent = Agent(name="agent")
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(ToolApprovalItem(agent=agent, raw_item=approved_call))
    context_wrapper.reject_tool(ToolApprovalItem(agent=agent, raw_item=rejected_call))

    runs = [
        ToolRunFunction(tool_call=approved_call, function_tool=approve_me),
        ToolRunFunction(tool_call=rejected_call, function_tool=approve_me),
    ]
    checker_calls: list[str] = []

    async def _needs_approval_checker(run: ToolRunFunction) -> bool:
        checker_calls.append(run.tool_call.call_id)
        raise AssertionError("checker must not run for resolved approvals")

    rejections: list[str | None] = []

    async def _record_rejection(
        call_id: str | None,
        _tool_call: ResponseFunctionToolCall,
        _tool: Any,
    ) -> None:
        rejections.append(call_id)

    selected = await _select_function_tool_runs_for_resume(
        runs,
        approval_items_by_call_id={},
        context_wrapper=context_wrapper,
        needs_approval_checker=_needs_approval_checker,
        output_exists_checker=lambda _run: False,
        record_rejection=_record_rejection,
        pending_interruption_adder=lambda _item: None,
        pending_item_builder=lambda run: ToolApprovalItem(agent=agent, raw_item=run.tool_call),
    )

    assert checker_calls == []
    assert [run.tool_call.call_id for run in selected] == ["approved-call"]
    assert rejections == ["rejected-call"]


@pytest.mark.asyncio
async def test_function_resume_reuses_falsy_pending_item() -> None:
    class FalsyToolApprovalItem(ToolApprovalItem):
        def __bool__(self) -> bool:
            return False

    @function_tool(needs_approval=True)
    async def approve_me(value: str) -> str:
        return value

    tool_call = make_function_tool_call(
        approve_me.name,
        call_id="pending-function",
        arguments='{"value":"a"}',
    )
    agent = Agent(name="agent", tools=[approve_me])
    existing_pending = FalsyToolApprovalItem(agent=agent, raw_item=tool_call)
    run = ToolRunFunction(tool_call=tool_call, function_tool=approve_me)
    pending: list[ToolApprovalItem] = []

    async def _needs_approval_checker(_run: ToolRunFunction) -> bool:
        return True

    async def _record_rejection(
        _call_id: str | None,
        _tool_call: ResponseFunctionToolCall,
        _tool: FunctionTool,
    ) -> None:
        raise AssertionError("unresolved approval must not be recorded as a rejection")

    selected = await _select_function_tool_runs_for_resume(
        [run],
        approval_items_by_call_id={tool_call.call_id: existing_pending},
        context_wrapper=make_context_wrapper(),
        needs_approval_checker=_needs_approval_checker,
        output_exists_checker=lambda _run: False,
        record_rejection=_record_rejection,
        pending_interruption_adder=pending.append,
        pending_item_builder=lambda _run: ToolApprovalItem(agent=agent, raw_item=tool_call),
    )

    assert selected == []
    assert pending == [existing_pending]
    assert pending[0] is existing_pending


@pytest.mark.asyncio
async def test_resume_rechecks_rejection_after_function_approval_checker() -> None:
    """A rejection recorded while the checker waits must prevent another interruption."""

    @function_tool(needs_approval=True)
    async def sensitive() -> str:
        return "should-not-run"

    tool_call = make_function_tool_call(sensitive.name, call_id="call-concurrent-function")
    run = ToolRunFunction(tool_call=tool_call, function_tool=sensitive)
    agent = Agent(name="agent", tools=[sensitive])
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)
    context_wrapper = make_context_wrapper()
    checker_started = asyncio.Event()
    release_checker = asyncio.Event()

    async def _needs_approval_checker(_run: ToolRunFunction) -> bool:
        checker_started.set()
        await release_checker.wait()
        return True

    pending: list[ToolApprovalItem] = []
    rejections: list[str | None] = []

    async def _record_rejection(
        call_id: str | None,
        _tool_call: ResponseFunctionToolCall,
        _tool: FunctionTool,
    ) -> None:
        rejections.append(call_id)

    selection_task = asyncio.create_task(
        _select_function_tool_runs_for_resume(
            [run],
            approval_items_by_call_id={tool_call.call_id: approval_item},
            context_wrapper=context_wrapper,
            needs_approval_checker=_needs_approval_checker,
            output_exists_checker=lambda _run: False,
            record_rejection=_record_rejection,
            pending_interruption_adder=pending.append,
            pending_item_builder=lambda _run: approval_item,
        )
    )
    try:
        await asyncio.wait_for(checker_started.wait(), timeout=1)
        context_wrapper.reject_tool(approval_item)
        release_checker.set()
        selected = await selection_task
    finally:
        release_checker.set()

    assert selected == []
    assert pending == []
    assert rejections == [tool_call.call_id]


@pytest.mark.asyncio
async def test_resume_rejects_changed_handoff_under_approved_function_call_id() -> None:
    """A resumed handoff must match the invocation that received approval."""
    target = Agent(name="target")
    route_handoff = handoff(target, tool_name_override="route")
    agent = Agent(name="agent", handoffs=[route_handoff])
    approved_call = make_function_tool_call(
        "route",
        call_id="call-shared",
        arguments='{"destination":"safe"}',
    )
    changed_call = make_function_tool_call(
        "route",
        call_id="call-shared",
        arguments='{"destination":"safe"}',
    )
    approval_item = ToolApprovalItem(agent=agent, raw_item=approved_call, tool_name="route")
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(approval_item, always_approve=True)
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[ToolRunHandoff(handoff=route_handoff, tool_call=changed_call)],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await _resolve_interrupted_turn(
            agent=agent,
            original_input="resume handoff",
            original_pre_step_items=[approval_item],
            new_response=ModelResponse(
                output=[changed_call],
                usage=Usage(),
                response_id="resp",
            ),
            processed_response=processed_response,
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            run_config=RunConfig(),
            run_state=make_state_with_interruptions(agent, [approval_item]),
        )


@pytest.mark.parametrize("approved", [True, False], ids=["approved", "rejected"])
@pytest.mark.asyncio
async def test_execute_path_prefers_decision_resolved_during_rejecting_guardrail(
    approved: bool,
) -> None:
    """Stored approval status must win when a rejecting guardrail was already waiting."""
    guardrail_started = asyncio.Event()
    release_guardrail = asyncio.Event()
    guardrail_calls = 0
    executed: list[str] = []

    @tool_input_guardrail
    async def rejecting_guardrail(
        _data: ToolInputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        nonlocal guardrail_calls
        guardrail_calls += 1
        guardrail_started.set()
        await release_guardrail.wait()
        return ToolGuardrailFunctionOutput.reject_content("guardrail rejection")

    @function_tool(needs_approval=True, tool_input_guardrails=[rejecting_guardrail])
    async def sensitive() -> str:
        executed.append("ran")
        return "tool output"

    tool_call = make_function_tool_call(sensitive.name, call_id="call-pending-guardrail")
    tool_run = ToolRunFunction(tool_call=tool_call, function_tool=sensitive)
    agent = Agent(name="agent", tools=[sensitive])
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)
    context_wrapper = make_context_wrapper()
    execution_task = asyncio.create_task(
        execute_function_tool_calls(
            bindings=bind_public_agent(agent),
            tool_runs=[tool_run],
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            config=RunConfig(
                tool_execution=ToolExecutionConfig(pre_approval_tool_input_guardrails=True)
            ),
        )
    )
    try:
        await asyncio.wait_for(guardrail_started.wait(), timeout=1)
        if approved:
            context_wrapper.approve_tool(approval_item)
        else:
            context_wrapper.reject_tool(approval_item, rejection_message="stored rejection")
        release_guardrail.set()
        results, _, _ = await execution_task
    finally:
        release_guardrail.set()

    assert len(results) == 1
    if approved:
        assert results[0].output == "guardrail rejection"
        assert guardrail_calls == 2
        assert executed == []
    else:
        assert results[0].output == "stored rejection"
        assert guardrail_calls == 1
        assert executed == []


@pytest.mark.asyncio
async def test_execute_path_skips_needs_approval_checker_when_status_resolved() -> None:
    """Resuming an approved call must not re-evaluate its dynamic approval policy."""
    checker_calls: list[str] = []

    async def needs_approval(_ctx: Any, _args: dict[str, Any], call_id: str) -> bool:
        checker_calls.append(call_id)
        if len(checker_calls) > 1:
            raise AssertionError("resolved approval must bypass needs_approval")
        return True

    @function_tool(needs_approval=needs_approval)
    async def sensitive(value: str) -> str:
        return f"ran:{value}"

    model = FakeModel()
    agent = Agent(name="agent", model=model, tools=[sensitive])
    model.add_multiple_turn_outputs(
        [
            [make_function_tool_call(sensitive.name, call_id="call-1", arguments='{"value":"x"}')],
            [get_text_message("done")],
        ]
    )

    first = await Runner.run(agent, "hello")
    assert len(first.interruptions) == 1
    assert checker_calls == ["call-1"]

    state = first.to_state()
    state.approve(first.interruptions[0])
    resumed = await Runner.run(agent, state)

    assert resumed.final_output == "done"
    assert checker_calls == ["call-1"]
    assert any(
        isinstance(item, ToolCallOutputItem) and item.output == "ran:x"
        for item in resumed.new_items
    )


@pytest.mark.asyncio
async def test_resume_checkpoints_tool_output_before_tool_use_behavior_failure() -> None:
    """A failed post-tool callback must leave the exact output replayable without reexecution."""
    executions: list[str] = []

    @function_tool(needs_approval=True)
    async def sensitive(value: str) -> str:
        executions.append(value)
        return f"ran:{value}"

    def failing_behavior(_ctx: Any, _results: Any) -> Any:
        raise RuntimeError("tool use behavior failed")

    model = FakeModel()
    agent = Agent(
        name="agent",
        model=model,
        tools=[sensitive],
        tool_use_behavior=failing_behavior,
    )
    model.add_multiple_turn_outputs(
        [
            [make_function_tool_call(sensitive.name, call_id="call-1", arguments='{"value":"x"}')],
            [get_text_message("done")],
        ]
    )

    first = await Runner.run(agent, "hello")
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(RuntimeError, match="tool use behavior failed"):
        await Runner.run(agent, state)

    assert executions == ["x"]
    assert any(
        isinstance(item, ToolCallOutputItem) and item.output == "ran:x"
        for item in state._generated_items
    )

    agent.tool_use_behavior = "run_llm_again"
    resumed = await Runner.run(agent, state)

    assert resumed.final_output == "done"
    assert executions == ["x"]


@pytest.mark.parametrize("tool_kind", ["function", "shell", "custom", "apply_patch"])
@pytest.mark.asyncio
async def test_execute_path_honors_sticky_rejection_before_checker(tool_kind: str) -> None:
    """A sticky rejection must bypass dynamic policies and prevent side effects."""
    executed: list[str] = []
    context_wrapper = make_context_wrapper()

    async def unexpected_checker(_ctx: Any, _payload: Any, _call_id: str) -> bool:
        raise AssertionError("sticky rejection must bypass needs_approval")

    if tool_kind == "function":

        @function_tool(needs_approval=unexpected_checker)
        async def sensitive() -> str:
            executed.append("function")
            return "should-not-run"

        agent = Agent(name="agent", tools=[sensitive])
        context_wrapper.reject_tool(
            ToolApprovalItem(
                agent=agent,
                raw_item=make_function_tool_call(sensitive.name, call_id="call-prior"),
            ),
            always_reject=True,
        )
        function_results, _, _ = await execute_function_tool_calls(
            bindings=bind_public_agent(agent),
            tool_runs=[
                ToolRunFunction(
                    tool_call=make_function_tool_call(sensitive.name, call_id="call-next"),
                    function_tool=sensitive,
                )
            ],
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            config=RunConfig(),
        )
        assert [result.output for result in function_results] == [HITL_REJECTION_MSG]
    elif tool_kind == "shell":

        def shell_executor(_req: Any) -> str:
            executed.append("shell")
            return "should-not-run"

        shell_tool = ShellTool(executor=shell_executor, needs_approval=unexpected_checker)
        agent = Agent(name="agent", tools=[shell_tool])
        context_wrapper.reject_tool(
            ToolApprovalItem(
                agent=agent,
                raw_item=cast(dict[str, Any], make_shell_call("call-prior")),
                tool_name=shell_tool.name,
            ),
            always_reject=True,
        )
        result = await ShellAction.execute(
            agent=agent,
            call=ToolRunShellCall(
                tool_call=cast(dict[str, Any], make_shell_call("call-next")),
                shell_tool=shell_tool,
            ),
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            config=RunConfig(),
        )
        assert isinstance(result, ToolCallOutputItem)
        assert HITL_REJECTION_MSG in str(result.output)
    elif tool_kind == "custom":

        async def invoke_custom(_ctx: Any, _raw: str) -> str:
            executed.append("custom")
            return "should-not-run"

        custom_tool = CustomTool(
            name="raw_editor",
            description="Edit raw text.",
            on_invoke_tool=invoke_custom,
            format={"type": "text"},
            needs_approval=unexpected_checker,
        )
        agent = Agent(name="agent", tools=[custom_tool])
        context_wrapper.reject_tool(
            ToolApprovalItem(
                agent=agent,
                raw_item=cast(
                    Any,
                    ResponseCustomToolCall(
                        type="custom_tool_call",
                        name=custom_tool.name,
                        call_id="call-prior",
                        input="prior",
                    ),
                ),
                tool_name=custom_tool.name,
            ),
            always_reject=True,
        )
        next_call = ResponseCustomToolCall(
            type="custom_tool_call",
            name=custom_tool.name,
            call_id="call-next",
            input="next",
        )
        result = await CustomToolAction.execute(
            agent=agent,
            call=ToolRunCustom(tool_call=next_call, custom_tool=custom_tool),
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            config=RunConfig(),
        )
        assert isinstance(result, ToolCallOutputItem)
        assert result.output == HITL_REJECTION_MSG
    else:
        editor = RecordingEditor()
        apply_patch_tool = ApplyPatchTool(
            editor=editor,
            needs_approval=unexpected_checker,
        )
        agent = Agent(name="agent", tools=[apply_patch_tool])
        context_wrapper.reject_tool(
            ToolApprovalItem(
                agent=agent,
                raw_item=cast(dict[str, Any], make_apply_patch_dict("call-prior")),
                tool_name=apply_patch_tool.name,
            ),
            always_reject=True,
        )
        result = await ApplyPatchAction.execute(
            agent=agent,
            call=ToolRunApplyPatchCall(
                tool_call=cast(dict[str, Any], make_apply_patch_dict("call-next")),
                apply_patch_tool=apply_patch_tool,
            ),
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            config=RunConfig(),
        )
        assert isinstance(result, ToolCallOutputItem)
        assert HITL_REJECTION_MSG in str(result.output)
        assert editor.operations == []

    assert executed == []


@pytest.mark.parametrize("tool_kind", ["shell", "custom", "apply_patch"])
@pytest.mark.asyncio
async def test_tool_execution_rejects_changed_approval_recorded_while_policy_waits(
    tool_kind: str,
) -> None:
    """A concurrent decision for changed content must not authorize the waiting call."""
    checker_started = asyncio.Event()
    release_checker = asyncio.Event()
    executed: list[str] = []
    context_wrapper = make_context_wrapper()
    tool: Any
    current_raw: Any
    changed_raw: Any
    execution_task: asyncio.Task[RunItem]

    async def needs_approval(_ctx: Any, _payload: Any, _call_id: str) -> bool:
        checker_started.set()
        await release_checker.wait()
        return True

    if tool_kind == "shell":

        def execute_shell(_request: Any) -> str:
            executed.append("shell")
            return "should-not-run"

        tool = ShellTool(executor=execute_shell, needs_approval=needs_approval)
        agent = Agent(name="agent", tools=[tool])
        current_raw = cast(dict[str, Any], make_shell_call("call-shared", commands=["safe"]))
        changed_raw = cast(dict[str, Any], make_shell_call("call-shared", commands=["changed"]))
        execution_task = asyncio.create_task(
            ShellAction.execute(
                agent=agent,
                call=ToolRunShellCall(tool_call=current_raw, shell_tool=tool),
                hooks=RunHooks(),
                context_wrapper=context_wrapper,
                config=RunConfig(),
            )
        )
    elif tool_kind == "custom":

        async def invoke_custom(_ctx: Any, _raw: str) -> str:
            executed.append("custom")
            return "should-not-run"

        tool = CustomTool(
            name="raw_editor",
            description="Edit raw text.",
            on_invoke_tool=invoke_custom,
            format={"type": "text"},
            needs_approval=needs_approval,
        )
        agent = Agent(name="agent", tools=[tool])
        current_raw = ResponseCustomToolCall(
            type="custom_tool_call",
            name=tool.name,
            call_id="call-shared",
            input="safe",
        )
        changed_raw = ResponseCustomToolCall(
            type="custom_tool_call",
            name=tool.name,
            call_id="call-shared",
            input="changed",
        )
        execution_task = asyncio.create_task(
            CustomToolAction.execute(
                agent=agent,
                call=ToolRunCustom(tool_call=current_raw, custom_tool=tool),
                hooks=RunHooks(),
                context_wrapper=context_wrapper,
                config=RunConfig(),
            )
        )
    else:
        editor = RecordingEditor()
        tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval)
        agent = Agent(name="agent", tools=[tool])
        current_raw = {
            "type": "apply_patch_call",
            "call_id": "call-shared",
            "operation": {"type": "delete_file", "path": "safe.txt"},
        }
        changed_raw = {
            "type": "apply_patch_call",
            "call_id": "call-shared",
            "operation": {"type": "delete_file", "path": "changed.txt"},
        }
        execution_task = asyncio.create_task(
            ApplyPatchAction.execute(
                agent=agent,
                call=ToolRunApplyPatchCall(tool_call=current_raw, apply_patch_tool=tool),
                hooks=RunHooks(),
                context_wrapper=context_wrapper,
                config=RunConfig(),
            )
        )

    try:
        await asyncio.wait_for(checker_started.wait(), timeout=1)
        context_wrapper.approve_tool(
            ToolApprovalItem(agent=agent, raw_item=changed_raw, tool_name=tool.name)
        )
        release_checker.set()
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await execution_task
    finally:
        release_checker.set()

    assert executed == []
    if tool_kind == "apply_patch":
        assert editor.operations == []


@pytest.mark.asyncio
async def test_collect_runs_by_approval_skips_checker_when_status_resolved() -> None:
    """Approved/rejected shell calls must not invoke needs_approval_checker.

    Mirrors #3229 for non-function tools: when the approval status is already
    True or False, a user-supplied checker (which may have side effects, hit
    the network, or raise) must be short-circuited.
    """
    shell_tool = ShellTool(executor=lambda _req: "ok", needs_approval=True)
    approved_call = make_shell_call("approved-shell")
    rejected_call = make_shell_call("rejected-shell")
    agent = Agent(name="agent")
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(
        ToolApprovalItem(
            agent=agent,
            raw_item=cast(dict[str, Any], approved_call),
            tool_name=shell_tool.name,
        )
    )
    context_wrapper.reject_tool(
        ToolApprovalItem(
            agent=agent,
            raw_item=cast(dict[str, Any], rejected_call),
            tool_name=shell_tool.name,
        )
    )

    runs = [
        ToolRunShellCall(tool_call=approved_call, shell_tool=shell_tool),
        ToolRunShellCall(tool_call=rejected_call, shell_tool=shell_tool),
    ]
    checker_calls: list[str] = []

    async def _needs_approval(run: ToolRunShellCall) -> bool:
        checker_calls.append(run.tool_call["call_id"])
        raise AssertionError("checker must not run for resolved approvals")

    async def _build_rejection(run: ToolRunShellCall, call_id: str) -> RunItem:
        return ToolCallOutputItem(
            output="rejected",
            raw_item={"type": "function_call_output", "call_id": call_id, "output": "rejected"},
            agent=agent,
        )

    approved, rejections = await _collect_runs_by_approval(
        runs,
        call_id_extractor=lambda run: run.tool_call["call_id"],
        tool_name_resolver=lambda run: run.shell_tool.name,
        rejection_builder=_build_rejection,
        context_wrapper=context_wrapper,
        approval_items_by_call_id={},
        agent=agent,
        pending_interruption_adder=lambda _item: None,
        needs_approval_checker=_needs_approval,
        output_exists_checker=lambda _call_id: False,
    )

    assert checker_calls == []
    assert approved == [runs[0]]
    assert len(rejections) == 1


@pytest.mark.asyncio
async def test_collect_runs_by_approval_reuses_falsy_pending_item() -> None:
    class FalsyToolApprovalItem(ToolApprovalItem):
        def __bool__(self) -> bool:
            return False

    shell_tool = ShellTool(executor=lambda _req: "ok", needs_approval=True)
    shell_call = make_shell_call("pending-shell")
    agent = Agent(name="agent")
    existing_pending = FalsyToolApprovalItem(
        agent=agent,
        raw_item=cast(dict[str, Any], shell_call),
        tool_name=shell_tool.name,
    )
    run = ToolRunShellCall(tool_call=shell_call, shell_tool=shell_tool)
    pending: list[ToolApprovalItem] = []

    async def _build_rejection(_run: ToolRunShellCall, call_id: str) -> RunItem:
        return ToolCallOutputItem(
            output="rejected",
            raw_item={"type": "function_call_output", "call_id": call_id, "output": "rejected"},
            agent=agent,
        )

    async def _needs_approval(_run: ToolRunShellCall) -> bool:
        return True

    approved, rejections = await _collect_runs_by_approval(
        [run],
        call_id_extractor=lambda item: item.tool_call["call_id"],
        tool_name_resolver=lambda item: item.shell_tool.name,
        rejection_builder=_build_rejection,
        context_wrapper=make_context_wrapper(),
        approval_items_by_call_id={"pending-shell": existing_pending},
        agent=agent,
        pending_interruption_adder=pending.append,
        needs_approval_checker=_needs_approval,
    )

    assert approved == []
    assert rejections == []
    assert pending == [existing_pending]
    assert pending[0] is existing_pending


@pytest.mark.parametrize("approved", [True, False], ids=["approved", "rejected"])
@pytest.mark.asyncio
async def test_resume_apply_patch_uses_concurrent_decision_without_reinterrupting(
    approved: bool,
) -> None:
    """A resolved apply-patch decision must stop callbacks and avoid stale interruptions."""
    checker_started = asyncio.Event()
    release_checker = asyncio.Event()
    checked_paths: list[str] = []

    async def _needs_approval(_ctx: Any, operation: Any, _call_id: str) -> bool:
        checked_paths.append(operation.path)
        if len(checked_paths) > 1:
            raise AssertionError("resolved rejection must stop later approval callbacks")
        checker_started.set()
        await release_checker.wait()
        return False

    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor, needs_approval=_needs_approval)
    _model, public_agent = make_model_and_agent(tools=[apply_patch_tool])
    execution_agent = public_agent.clone()
    set_public_agent(execution_agent, public_agent)
    raw_item = cast(
        Any,
        {
            "type": "apply_patch_call",
            "call_id": "call-concurrent-apply-patch",
            "operations": [
                {"type": "update_file", "path": "first.txt", "diff": "-old\n+new\n"},
                {"type": "delete_file", "path": "second.txt"},
            ],
        },
    )
    approval_item = ToolApprovalItem(
        agent=public_agent,
        raw_item=raw_item,
        tool_name=apply_patch_tool.name,
    )
    context_wrapper = make_context_wrapper()
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[
            ToolRunApplyPatchCall(tool_call=raw_item, apply_patch_tool=apply_patch_tool)
        ],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    resolution_task = asyncio.create_task(
        _resolve_interrupted_turn(
            agent=execution_agent,
            original_input="resume apply patch",
            original_pre_step_items=[],
            new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
            processed_response=processed_response,
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            run_config=RunConfig(),
            run_state=make_state_with_interruptions(public_agent, [approval_item]),
        )
    )
    try:
        await asyncio.wait_for(checker_started.wait(), timeout=1)
        if approved:
            context_wrapper.approve_tool(approval_item)
        else:
            context_wrapper.reject_tool(approval_item)
        release_checker.set()
        result = await resolution_task
    finally:
        release_checker.set()

    assert checked_paths == ["first.txt"]
    assert not isinstance(result.next_step, NextStepInterruption)
    rejection_outputs = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem) and item.output == HITL_REJECTION_MSG
    ]
    if approved:
        assert rejection_outputs == []
        assert len(editor.operations) == 2
    else:
        assert len(rejection_outputs) == 1
        assert editor.operations == []


@pytest.mark.parametrize("tool_kind", ["shell", "apply_patch"])
@pytest.mark.asyncio
async def test_resume_preserves_approval_created_during_tool_execution(tool_kind: str) -> None:
    """A second policy evaluation may create a new approval interruption during execution."""
    checker_calls = 0
    executed: list[str] = []
    context_wrapper = make_context_wrapper()
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    async def needs_approval(_ctx: Any, _payload: Any, _call_id: str) -> bool:
        nonlocal checker_calls
        checker_calls += 1
        return checker_calls == 2

    tool: Any
    if tool_kind == "shell":

        def execute_shell(_request: Any) -> str:
            executed.append("shell")
            return "should-not-run"

        tool = ShellTool(executor=execute_shell, needs_approval=needs_approval)
        raw_item = cast(dict[str, Any], make_shell_call("call-execution-approval"))
        processed_response.shell_calls = [ToolRunShellCall(tool_call=raw_item, shell_tool=tool)]
    else:
        editor = RecordingEditor()
        tool = ApplyPatchTool(editor=editor, needs_approval=needs_approval)
        raw_item = cast(Any, make_apply_patch_dict("call-execution-approval"))
        processed_response.apply_patch_calls = [
            ToolRunApplyPatchCall(tool_call=raw_item, apply_patch_tool=tool)
        ]

    _model, public_agent = make_model_and_agent(tools=[tool])
    execution_agent = public_agent.clone()
    set_public_agent(execution_agent, public_agent)
    original_approval = ToolApprovalItem(
        agent=public_agent,
        raw_item=raw_item,
        tool_name=tool.name,
    )

    result = await _resolve_interrupted_turn(
        agent=execution_agent,
        original_input="resume approval",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=make_state_with_interruptions(public_agent, [original_approval]),
    )

    assert checker_calls == 2
    assert isinstance(result.next_step, NextStepInterruption)
    assert [extract_tool_call_id(item.raw_item) for item in result.next_step.interruptions] == [
        "call-execution-approval"
    ]
    assert executed == []
    if tool_kind == "apply_patch":
        assert editor.operations == []


@pytest.mark.asyncio
async def test_resume_rebuilds_function_runs_from_object_approvals() -> None:
    """Rebuild should handle ResponseFunctionToolCall approval items."""

    @function_tool(needs_approval=True)
    def approve_me(reason: Optional[str] = None) -> str:  # noqa: UP007
        return f"approved:{reason}" if reason else "approved"

    model, agent = make_model_and_agent(tools=[approve_me])
    tool_call = make_function_tool_call(
        approve_me.name,
        call_id="call-rebuild-obj",
        arguments='{"reason": "ok"}',
    )
    assert isinstance(tool_call, ResponseFunctionToolCall)
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call)
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(approval_item)

    run_state = make_state_with_interruptions(agent, [approval_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert not isinstance(result.next_step, NextStepInterruption)
    executed_call_ids = {
        extract_tool_call_id(item.raw_item)
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem)
    }
    assert "call-rebuild-obj" in executed_call_ids, (
        "Function should be rebuilt from ResponseFunctionToolCall approval"
    )


@pytest.mark.asyncio
async def test_resume_rebuilds_local_mcp_function_runs_from_approvals() -> None:
    """Rebuild should resolve approved MCP-backed function tools from agent.mcp_servers."""

    server = FakeMCPServer(require_approval="always")
    server.add_tool("add", {"type": "object", "properties": {}})

    agent = Agent(name="TestAgent", mcp_servers=[server])
    tool_call = make_function_tool_call(
        "add",
        call_id="call-mcp-rebuild",
        arguments='{"value": 1}',
    )
    approval_item = ToolApprovalItem(agent=agent, raw_item=tool_call, tool_name="add")
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(approval_item)

    run_state = make_state_with_interruptions(agent, [approval_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert not isinstance(result.next_step, NextStepInterruption)
    assert server.tool_calls == ["add"]
    executed_call_ids = {
        extract_tool_call_id(item.raw_item)
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem)
    }
    assert "call-mcp-rebuild" in executed_call_ids, (
        "Approved local MCP tool should be rebuilt and executed from pending approvals"
    )


@pytest.mark.asyncio
async def test_resume_rebuild_rejections_use_deferred_tool_display_name() -> None:
    """Resume-time rejection formatting should collapse synthetic deferred namespaces."""

    async def get_weather() -> str:
        return "sunny"

    _model, agent = make_model_and_agent(
        tools=[function_tool(get_weather, name_override="get_weather", defer_loading=True)]
    )
    context_wrapper = make_context_wrapper()

    rejected_call = make_function_tool_call(
        "get_weather",
        call_id="call-deferred-reject",
        namespace="get_weather",
    )
    assert isinstance(rejected_call, ResponseFunctionToolCall)

    rejected_item = ToolApprovalItem(
        agent=agent,
        raw_item=rejected_call,
        tool_name="get_weather",
        tool_namespace="get_weather",
    )
    context_wrapper.reject_tool(rejected_item)

    run_state = make_state_with_interruptions(agent, [rejected_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(
            tool_error_formatter=lambda args: (
                f"resume-level {args.tool_name} denied ({args.call_id})"
            )
        ),
        run_state=run_state,
    )

    rejection_outputs = [
        item.output for item in result.new_step_items if isinstance(item, ToolCallOutputItem)
    ]
    assert rejection_outputs == ["resume-level get_weather denied (call-deferred-reject)"]


@pytest.mark.asyncio
async def test_rebuild_function_runs_handles_object_pending_and_rejections() -> None:
    """Rebuild should surface pending approvals and emit rejections for object approvals."""

    @function_tool(needs_approval=True)
    def reject_me(text: str = "nope") -> str:
        return text

    @function_tool(needs_approval=True)
    def pending_me(text: str = "wait") -> str:
        return text

    _model, agent = make_model_and_agent(tools=[reject_me, pending_me])
    context_wrapper = make_context_wrapper()

    rejected_call = make_function_tool_call(reject_me.name, call_id="obj-reject")
    pending_call = make_function_tool_call(pending_me.name, call_id="obj-pending")
    assert isinstance(rejected_call, ResponseFunctionToolCall)
    assert isinstance(pending_call, ResponseFunctionToolCall)

    rejected_item = ToolApprovalItem(agent=agent, raw_item=rejected_call)
    pending_item = ToolApprovalItem(agent=agent, raw_item=pending_call)
    context_wrapper.reject_tool(rejected_item)

    run_state = make_state_with_interruptions(agent, [rejected_item, pending_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert isinstance(result.next_step, NextStepInterruption)
    assert pending_item in result.next_step.interruptions
    rejection_outputs = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem) and item.output == HITL_REJECTION_MSG
    ]
    assert rejection_outputs, "Rejected function call should emit rejection output"


@pytest.mark.asyncio
async def test_resume_function_rejection_outputs_use_public_agent() -> None:
    @function_tool(needs_approval=True)
    def reject_me(text: str = "nope") -> str:
        return text

    _model, public_agent = make_model_and_agent(tools=[reject_me])
    execution_agent = public_agent.clone()
    set_public_agent(execution_agent, public_agent)
    context_wrapper = make_context_wrapper()

    rejected_call = make_function_tool_call(reject_me.name, call_id="obj-reject-public")
    assert isinstance(rejected_call, ResponseFunctionToolCall)
    rejected_item = ToolApprovalItem(agent=public_agent, raw_item=rejected_call)
    context_wrapper.reject_tool(rejected_item)

    run_state = make_state_with_interruptions(public_agent, [rejected_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=execution_agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    rejection_outputs = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem) and item.output == HITL_REJECTION_MSG
    ]
    assert rejection_outputs
    assert all(item.agent is public_agent for item in rejection_outputs)


@pytest.mark.parametrize("tool_kind", ["shell", "apply_patch"])
@pytest.mark.asyncio
async def test_resume_non_function_rejection_outputs_use_public_agent(
    tool_kind: str,
) -> None:
    context_wrapper = make_context_wrapper()
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    if tool_kind == "shell":
        shell_tool = ShellTool(executor=lambda _req: "should_not_run", needs_approval=True)
        _model, public_agent = make_model_and_agent(tools=[shell_tool])
        raw_item = cast(
            dict[str, Any],
            make_shell_call(
                "call_reject_shell_public",
                id_value="shell_reject_public",
                commands=["echo test"],
                status="in_progress",
            ),
        )
        processed_response.shell_calls = [
            ToolRunShellCall(tool_call=raw_item, shell_tool=shell_tool)
        ]
        tool_name = shell_tool.name
    else:
        apply_patch_tool = ApplyPatchTool(editor=RecordingEditor(), needs_approval=True)
        _model, public_agent = make_model_and_agent(tools=[apply_patch_tool])
        raw_item = cast(Any, make_apply_patch_dict("call_apply_reject_public"))
        processed_response.apply_patch_calls = [
            ToolRunApplyPatchCall(tool_call=raw_item, apply_patch_tool=apply_patch_tool)
        ]
        tool_name = apply_patch_tool.name

    execution_agent = public_agent.clone()
    set_public_agent(execution_agent, public_agent)
    approval_item = ToolApprovalItem(agent=public_agent, raw_item=raw_item, tool_name=tool_name)
    context_wrapper.reject_tool(approval_item)

    result = await _resolve_interrupted_turn(
        agent=execution_agent,
        original_input="resume rejection",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=make_state_with_interruptions(public_agent, [approval_item]),
    )

    rejection_outputs = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem) and item.output == HITL_REJECTION_MSG
    ]
    assert rejection_outputs
    assert all(item.agent is public_agent for item in rejection_outputs)


@pytest.mark.asyncio
async def test_resume_keeps_unmatched_pending_approvals_with_function_runs() -> None:
    """Pending approvals should persist even when resume has other function runs."""

    @function_tool
    def outer_tool() -> str:
        return "outer"

    @function_tool(needs_approval=True)
    def inner_tool() -> str:
        return "inner"

    _model, agent = make_model_and_agent(tools=[outer_tool, inner_tool])
    context_wrapper = make_context_wrapper()

    pending_call = make_function_tool_call(inner_tool.name, call_id="call-inner")
    assert isinstance(pending_call, ResponseFunctionToolCall)
    pending_item = ToolApprovalItem(agent=agent, raw_item=pending_call)

    run_state = make_state_with_interruptions(agent, [pending_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[
            ToolRunFunction(
                tool_call=make_function_tool_call(outer_tool.name, call_id="call-outer"),
                function_tool=outer_tool,
            )
        ],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert isinstance(result.next_step, NextStepInterruption)
    assert pending_item in result.next_step.interruptions


@pytest.mark.asyncio
async def test_resume_executes_non_hitl_function_calls_without_output() -> None:
    """Non-HITL function calls should run on resume when no output exists."""

    @function_tool
    def already_ran() -> str:
        return "done"

    _, agent = make_model_and_agent(tools=[already_ran])
    function_call = make_function_tool_call(already_ran.name, call_id="call-skip")

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[ToolRunFunction(tool_call=function_call, function_tool=already_ran)],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume run",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=make_context_wrapper(),
        run_config=RunConfig(),
        run_state=None,
    )

    assert isinstance(result.next_step, NextStepRunAgain)
    assert any(
        isinstance(item, ToolCallOutputItem) and item.output == "done"
        for item in result.new_step_items
    ), "Non-HITL tools should run on resume when output is missing"


@pytest.mark.asyncio
async def test_resume_skips_non_hitl_function_calls_with_existing_output() -> None:
    """Non-HITL function calls with persisted outputs should not re-run on resume."""

    @function_tool
    def already_ran() -> str:
        return "done"

    model, agent = make_model_and_agent(tools=[already_ran])
    function_call = make_function_tool_call(already_ran.name, call_id="call-skip")

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[ToolRunFunction(tool_call=function_call, function_tool=already_ran)],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(
        ToolApprovalItem(agent=agent, raw_item=function_call, tool_name=already_ran.name),
        always_approve=True,
    )

    original_pre_step_items: list[RunItem] = [
        ToolCallOutputItem(
            agent=agent,
            raw_item={
                "type": "function_call_output",
                "call_id": "call-skip",
                "output": "prior run",
            },
            output="prior run",
        )
    ]

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume run",
        original_pre_step_items=original_pre_step_items,
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=None,
    )

    assert isinstance(result.next_step, NextStepRunAgain)
    assert not result.new_step_items, "Existing outputs should prevent re-execution on resume"


@pytest.mark.asyncio
async def test_resume_skips_shell_calls_with_existing_output() -> None:
    """Shell calls with persisted output should not execute a second time when resuming."""

    shell_tool = ShellTool(executor=lambda _req: "should_not_run", needs_approval=True)
    model, agent = make_model_and_agent(tools=[shell_tool])

    shell_call = make_shell_call(
        "call_shell_resume", id_value="shell_resume", commands=["echo done"], status="completed"
    )
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[ToolRunShellCall(tool_call=shell_call, shell_tool=shell_tool)],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    original_pre_step_items = [
        ToolCallOutputItem(
            agent=agent,
            raw_item=cast(
                dict[str, Any],
                {
                    "type": "shell_call_output",
                    "call_id": "call_shell_resume",
                    "status": "completed",
                    "output": "prior run",
                },
            ),
            output="prior run",
        )
    ]

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume shell",
        original_pre_step_items=cast(list[RunItem], original_pre_step_items),
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=make_context_wrapper(),
        run_config=RunConfig(),
        run_state=None,
    )

    assert isinstance(result.next_step, NextStepRunAgain)
    assert not result.new_step_items, "Shell call should not run when output already exists"


@pytest.mark.asyncio
async def test_resume_validates_changed_shell_before_sibling_approval_callback() -> None:
    """Changed completed calls must fail before sibling approval callbacks run."""
    checker_calls: list[str] = []

    async def needs_approval(_ctx: Any, _args: dict[str, Any], call_id: str) -> bool:
        checker_calls.append(call_id)
        return False

    @function_tool(needs_approval=needs_approval)
    async def sibling_tool() -> str:
        return "should-not-run"

    shell_tool = ShellTool(executor=lambda _request: "should-not-run", needs_approval=True)
    enabled_calls: list[str] = []
    target = Agent(name="target")

    def handoff_is_enabled(_ctx: Any, _agent: Agent[Any]) -> bool:
        enabled_calls.append("handoff")
        return True

    agent = Agent(
        name="agent",
        tools=[sibling_tool, shell_tool],
        handoffs=[handoff(target, is_enabled=handoff_is_enabled)],
    )
    context_wrapper = make_context_wrapper()
    approved_shell_call = cast(
        dict[str, Any],
        make_shell_call("call-reused", commands=["echo safe"], status="completed"),
    )
    changed_shell_call = cast(
        dict[str, Any],
        make_shell_call("call-reused", commands=["echo changed"], status="completed"),
    )
    context_wrapper.approve_tool(
        ToolApprovalItem(
            agent=agent,
            raw_item=approved_shell_call,
            tool_name=shell_tool.name,
        )
    )
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[
            ToolRunFunction(
                tool_call=make_function_tool_call(sibling_tool.name, call_id="call-sibling"),
                function_tool=sibling_tool,
            )
        ],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[
            ToolRunShellCall(tool_call=changed_shell_call, shell_tool=shell_tool),
        ],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )
    original_pre_step_items = [
        ToolCallOutputItem(
            agent=agent,
            raw_item=cast(
                dict[str, Any],
                {
                    "type": "shell_call_output",
                    "call_id": "call-reused",
                    "status": "completed",
                    "output": "prior run",
                },
            ),
            output="prior run",
        )
    ]

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await _resolve_interrupted_turn(
            agent=agent,
            original_input="resume run",
            original_pre_step_items=cast(list[RunItem], original_pre_step_items),
            new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
            processed_response=processed_response,
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            run_config=RunConfig(),
            run_state=None,
        )

    assert checker_calls == []
    assert enabled_calls == []


@pytest.mark.asyncio
async def test_resume_keeps_approved_shell_outputs_with_pending_interruptions() -> None:
    """Approved shell outputs should be emitted even when other approvals are still pending."""

    @function_tool(needs_approval=True)
    def pending_tool() -> str:
        return "ok"

    shell_tool = ShellTool(executor=lambda _req: "shell-ok", needs_approval=True)
    _model, agent = make_model_and_agent(tools=[pending_tool, shell_tool])
    context_wrapper = make_context_wrapper()

    function_call = make_function_tool_call(pending_tool.name, call_id="call-pending")
    shell_call = make_shell_call(
        "call_shell_ok", id_value="shell_ok", commands=["echo ok"], status="completed"
    )

    shell_approval = ToolApprovalItem(
        agent=agent,
        raw_item=cast(dict[str, Any], shell_call),
        tool_name=shell_tool.name,
    )
    context_wrapper.approve_tool(shell_approval)

    pending_approval = ToolApprovalItem(
        agent=agent,
        raw_item=function_call,
        tool_name=pending_tool.name,
    )
    run_state = make_state_with_interruptions(agent, [pending_approval])

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[ToolRunFunction(function_tool=pending_tool, tool_call=function_call)],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[ToolRunShellCall(tool_call=shell_call, shell_tool=shell_tool)],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume shell with pending approval",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert isinstance(result.next_step, NextStepInterruption)
    shell_outputs = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "shell_call_output"
        and item.raw_item.get("call_id") == "call_shell_ok"
    ]
    assert shell_outputs, "Approved shell output should be included with pending interruptions"


@pytest.mark.asyncio
async def test_resume_executes_pending_computer_actions() -> None:
    """Pending computer actions should execute when resuming an interrupted turn."""

    computer = TrackingComputer()
    computer_tool = ComputerTool(computer=computer)
    model, agent = make_model_and_agent(tools=[computer_tool])

    computer_call = ResponseComputerToolCall(
        type="computer_call",
        id="comp_pending",
        call_id="comp_pending",
        status="in_progress",
        action=ActionScreenshot(type="screenshot"),
        pending_safety_checks=[],
    )

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[
            ToolRunComputerAction(tool_call=computer_call, computer_tool=computer_tool)
        ],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[computer_tool.name],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume computer",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=make_context_wrapper(),
        run_config=RunConfig(),
        run_state=None,
    )

    outputs = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "computer_call_output"
    ]
    assert outputs, "Computer action should run when resuming without prior output"
    assert computer.calls, "Computer should have been invoked"
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_resume_checkpoints_computer_output_before_custom_data_failure() -> None:
    """A failed extractor must not make a completed computer side effect retryable."""

    computer = TrackingComputer()

    def fail_custom_data(_context: Any) -> dict[str, Any]:
        raise RuntimeError("custom data failed")

    computer_tool = ComputerTool(
        computer=computer,
        custom_data_extractor=fail_custom_data,
    )
    _model, agent = make_model_and_agent(tools=[computer_tool])
    computer_call = ResponseComputerToolCall(
        type="computer_call",
        id="comp_checkpoint",
        call_id="comp_checkpoint",
        status="in_progress",
        action=ActionScreenshot(type="screenshot"),
        pending_safety_checks=[],
    )
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[
            ToolRunComputerAction(tool_call=computer_call, computer_tool=computer_tool)
        ],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[computer_tool.name],
        mcp_approval_requests=[],
        interruptions=[],
    )
    context_wrapper = make_context_wrapper()
    run_state = make_state_with_interruptions(agent, [])
    run_state._context = context_wrapper

    with pytest.raises(RuntimeError, match="custom data failed"):
        await _resolve_interrupted_turn(
            agent=agent,
            original_input="resume computer",
            original_pre_step_items=[],
            new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
            processed_response=processed_response,
            hooks=RunHooks(),
            context_wrapper=context_wrapper,
            run_config=RunConfig(),
            run_state=run_state,
        )

    checkpointed_items = list(run_state._generated_items)
    assert len(checkpointed_items) == 1
    assert isinstance(checkpointed_items[0], ToolCallOutputItem)
    assert checkpointed_items[0].call_id == "comp_checkpoint"
    assert checkpointed_items[0].custom_data is None

    computer_tool.custom_data_extractor = None
    resumed = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume computer",
        original_pre_step_items=checkpointed_items,
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert computer.calls == ["screenshot"]
    assert resumed.new_step_items == []
    assert run_state._generated_items == checkpointed_items
    assert isinstance(resumed.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_resume_skips_computer_actions_with_existing_output() -> None:
    """Computer actions with persisted output should not execute again when resuming."""

    computer = TrackingComputer()
    computer_tool = ComputerTool(computer=computer)
    model, agent = make_model_and_agent(tools=[computer_tool])

    computer_call = ResponseComputerToolCall(
        type="computer_call",
        id="comp_skip",
        call_id="comp_skip",
        status="completed",
        action=ActionScreenshot(type="screenshot"),
        pending_safety_checks=[],
    )

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[
            ToolRunComputerAction(tool_call=computer_call, computer_tool=computer_tool)
        ],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[computer_tool.name],
        mcp_approval_requests=[],
        interruptions=[],
    )

    original_pre_step_items = [
        ToolCallOutputItem(
            agent=agent,
            raw_item={
                "type": "computer_call_output",
                "call_id": "comp_skip",
                "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,ok"},
            },
            output="image_url",
        )
    ]

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume computer existing",
        original_pre_step_items=cast(list[RunItem], original_pre_step_items),
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=make_context_wrapper(),
        run_config=RunConfig(),
        run_state=None,
    )

    assert not computer.calls, "Computer action should not run when output already exists"
    assert not result.new_step_items, "No new items should be emitted when output exists"
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_rebuild_function_runs_handles_pending_and_rejections() -> None:
    """Rebuilt function runs should surface pending approvals and emit rejections."""

    class FalsyToolApprovalItem(ToolApprovalItem):
        def __bool__(self) -> bool:
            return False

    @function_tool(needs_approval=True)
    def reject_me(text: str = "nope") -> str:
        return text

    @function_tool(needs_approval=True)
    def pending_me(text: str = "wait") -> str:
        return text

    _model, agent = make_model_and_agent(tools=[reject_me, pending_me])
    context_wrapper = make_context_wrapper()

    rejected_raw = {
        "type": "function_call",
        "name": reject_me.name,
        "call_id": "call-reject",
        "arguments": "{}",
    }
    pending_raw = {
        "type": "function_call",
        "name": pending_me.name,
        "call_id": "call-pending",
        "arguments": "{}",
    }

    rejected_item = ToolApprovalItem(agent=agent, raw_item=rejected_raw)
    pending_item = FalsyToolApprovalItem(agent=agent, raw_item=pending_raw)
    context_wrapper.reject_tool(rejected_item)

    run_state = make_state_with_interruptions(agent, [rejected_item, pending_item])
    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert isinstance(result.next_step, NextStepInterruption)
    assert any(item is pending_item for item in result.next_step.interruptions)
    assert any(item is pending_item for item in result.new_step_items)
    rejection_outputs = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem) and item.output == HITL_REJECTION_MSG
    ]
    assert rejection_outputs, "Rejected function call should emit rejection output"


@pytest.mark.parametrize(
    "raw_item, tool_name",
    [
        (
            make_shell_call(
                "call_shell_pending_rebuild",
                id_value="shell_pending_rebuild",
                commands=["echo pending"],
            ),
            "shell",
        ),
        (cast(Any, make_apply_patch_dict("call_apply_pending_rebuild")), "apply_patch"),
        (
            {
                "type": "function_call",
                "name": "missing_tool",
                "call_id": "call_missing_tool",
                "arguments": "{}",
            },
            "missing_tool",
        ),
    ],
    ids=["shell", "apply_patch", "missing_function_tool"],
)
@pytest.mark.asyncio
async def test_rebuild_preserves_unmatched_pending_approvals(
    raw_item: Any,
    tool_name: str,
) -> None:
    """Unmatched pending approvals should remain interruptions when rebuilding."""
    _model, agent = make_model_and_agent()
    approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name=tool_name)
    run_state = make_state_with_interruptions(agent, [approval_item])
    context_wrapper = make_context_wrapper()

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume approvals",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    assert isinstance(result.next_step, NextStepInterruption)
    assert approval_item in result.next_step.interruptions


@pytest.mark.asyncio
async def test_rejected_shell_calls_emit_rejection_output() -> None:
    """Shell calls should produce rejection output when already denied."""

    shell_tool = ShellTool(executor=lambda _req: "should_not_run", needs_approval=True)
    _model, agent = make_model_and_agent(tools=[shell_tool])
    context_wrapper = make_context_wrapper()

    shell_call = make_shell_call(
        "call_reject_shell", id_value="shell_reject", commands=["echo test"], status="in_progress"
    )
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item=cast(dict[str, Any], shell_call),
        tool_name=shell_tool.name,
    )
    context_wrapper.reject_tool(approval_item)

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[ToolRunShellCall(tool_call=shell_call, shell_tool=shell_tool)],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume shell rejection",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=make_state_with_interruptions(agent, [approval_item]),
    )

    rejection_outputs: list[ToolCallOutputItem] = []
    for item in result.new_step_items:
        if not isinstance(item, ToolCallOutputItem):
            continue
        raw = item.raw_item
        if not isinstance(raw, dict) or raw.get("type") != "shell_call_output":
            continue
        output_value = cast(list[dict[str, Any]], raw.get("output") or [])
        if not output_value:
            continue
        first_entry = output_value[0]
        if first_entry.get("stderr") == HITL_REJECTION_MSG:
            rejection_outputs.append(item)
    assert rejection_outputs, "Rejected shell call should yield rejection output"
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_rejected_shell_calls_with_existing_output_are_not_duplicated() -> None:
    """Rejected shell calls with persisted output should not emit duplicate rejections."""

    shell_tool = ShellTool(executor=lambda _req: "should_not_run", needs_approval=True)
    _model, agent = make_model_and_agent(tools=[shell_tool])
    context_wrapper = make_context_wrapper()

    shell_call = make_shell_call(
        "call_reject_shell_dup",
        id_value="shell_reject_dup",
        commands=["echo test"],
        status="in_progress",
    )
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item=cast(dict[str, Any], shell_call),
        tool_name=shell_tool.name,
    )
    context_wrapper.reject_tool(approval_item)

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[ToolRunShellCall(tool_call=shell_call, shell_tool=shell_tool)],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    original_pre_step_items = [
        ToolCallOutputItem(
            agent=agent,
            raw_item=cast(
                dict[str, Any],
                {
                    "type": "shell_call_output",
                    "call_id": "call_reject_shell_dup",
                    "output": [
                        {
                            "stdout": "",
                            "stderr": HITL_REJECTION_MSG,
                            "outcome": {"type": "exit", "exit_code": 1},
                        }
                    ],
                },
            ),
            output=HITL_REJECTION_MSG,
        )
    ]

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="resume shell rejection existing",
        original_pre_step_items=cast(list[RunItem], original_pre_step_items),
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=None,
    )

    duplicate_rejections = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "shell_call_output"
        and HITL_REJECTION_MSG in str(item.output)
    ]

    assert not duplicate_rejections, "No duplicate rejection outputs should be emitted"
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_mcp_callback_approvals_are_processed() -> None:
    """MCP approval requests with callbacks should emit approval responses."""

    agent = make_agent()
    context_wrapper = make_context_wrapper()

    class DummyMcpTool:
        def __init__(self) -> None:
            self.on_approval_request = lambda _req: {"approve": True, "reason": "ok"}

    approval_request = ToolRunMCPApprovalRequest(
        request_item=McpApprovalRequest(
            id="mcp-callback-1",
            type="mcp_approval_request",
            server_label="server",
            arguments="{}",
            name="hosted_mcp",
        ),
        mcp_tool=cast(HostedMCPTool, DummyMcpTool()),
    )

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[approval_request],
        interruptions=[],
    )

    result = await _resolve_interrupted_turn(
        agent=agent,
        original_input="handle mcp",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=None,
    )

    assert any(
        isinstance(item, MCPApprovalResponseItem) and item.raw_item.get("approve") is True
        for item in result.new_step_items
    ), "MCP callback approvals should emit approval responses"
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_mcp_callback_exact_retry_reuses_stored_decision() -> None:
    """An exact uncommitted MCP retry must not invoke the approval callback twice."""
    callback_calls: list[str] = []
    agent = make_agent()
    context_wrapper = make_context_wrapper()

    class DummyMcpTool:
        def on_approval_request(self, request: Any) -> dict[str, Any]:
            callback_calls.append(request.data.id)
            return {"approve": True, "reason": "ok"}

    approval_request = ToolRunMCPApprovalRequest(
        request_item=McpApprovalRequest(
            id="mcp-callback-retry",
            type="mcp_approval_request",
            server_label="server",
            arguments="{}",
            name="hosted_mcp",
        ),
        mcp_tool=cast(HostedMCPTool, DummyMcpTool()),
    )

    first = await execute_mcp_approval_requests(
        agent=agent,
        approval_requests=[approval_request],
        context_wrapper=context_wrapper,
    )
    second = await execute_mcp_approval_requests(
        agent=agent,
        approval_requests=[approval_request],
        context_wrapper=context_wrapper,
    )

    assert callback_calls == ["mcp-callback-retry"]
    responses = [item for item in [*first, *second] if isinstance(item, MCPApprovalResponseItem)]
    assert [item.raw_item["approve"] for item in responses] == [True, True]


@pytest.mark.asyncio
async def test_mcp_callback_failure_is_not_retried_for_same_request() -> None:
    """A callback that started without a committed response fails closed on retry."""
    callback_calls: list[str] = []
    agent = make_agent()
    context_wrapper = make_context_wrapper()

    class DummyMcpTool:
        def on_approval_request(self, request: Any) -> dict[str, Any]:
            callback_calls.append(request.data.id)
            raise RuntimeError("callback failed")

    approval_request = ToolRunMCPApprovalRequest(
        request_item=McpApprovalRequest(
            id="mcp-callback-failure",
            type="mcp_approval_request",
            server_label="server",
            arguments="{}",
            name="hosted_mcp",
        ),
        mcp_tool=cast(HostedMCPTool, DummyMcpTool()),
    )

    with pytest.raises(RuntimeError, match="callback failed"):
        await execute_mcp_approval_requests(
            agent=agent,
            approval_requests=[approval_request],
            context_wrapper=context_wrapper,
        )
    with pytest.raises(ModelBehaviorError, match="already ran"):
        await execute_mcp_approval_requests(
            agent=agent,
            approval_requests=[approval_request],
            context_wrapper=context_wrapper,
        )

    assert callback_calls == ["mcp-callback-failure"]


@pytest.mark.asyncio
async def test_mcp_callback_exact_siblings_invoke_callback_once() -> None:
    """Exact same-ID MCP siblings must share one approval callback result."""
    callback_calls: list[str] = []
    agent = make_agent()
    context_wrapper = make_context_wrapper()

    class DummyMcpTool:
        async def on_approval_request(self, request: Any) -> dict[str, Any]:
            callback_calls.append(request.data.id)
            await asyncio.sleep(0)
            return {"approve": True, "reason": "ok"}

    mcp_tool = cast(HostedMCPTool, DummyMcpTool())
    approval_requests = [
        ToolRunMCPApprovalRequest(
            request_item=McpApprovalRequest(
                id="mcp-callback-sibling",
                type="mcp_approval_request",
                server_label="server",
                arguments="{}",
                name="hosted_mcp",
            ),
            mcp_tool=mcp_tool,
        )
        for _ in range(2)
    ]

    responses = await execute_mcp_approval_requests(
        agent=agent,
        approval_requests=approval_requests,
        context_wrapper=context_wrapper,
    )

    assert callback_calls == ["mcp-callback-sibling"]
    assert len(responses) == 1


@pytest.mark.asyncio
async def test_mcp_callback_changed_same_id_siblings_fail_before_callbacks() -> None:
    """Changed same-ID MCP siblings must fail before invoking approval callbacks."""
    callback_calls: list[str] = []
    agent = make_agent()
    context_wrapper = make_context_wrapper()

    class DummyMcpTool:
        async def on_approval_request(self, request: Any) -> dict[str, Any]:
            callback_calls.append(request.data.arguments)
            await asyncio.sleep(0)
            return {"approve": True, "reason": "ok"}

    mcp_tool = cast(HostedMCPTool, DummyMcpTool())
    approval_requests = [
        ToolRunMCPApprovalRequest(
            request_item=McpApprovalRequest(
                id="mcp-callback-sibling",
                type="mcp_approval_request",
                server_label="server",
                arguments=arguments,
                name="hosted_mcp",
            ),
            mcp_tool=mcp_tool,
        )
        for arguments in ('{"q":1}', '{"q":2}')
    ]

    with pytest.raises(ModelBehaviorError, match="reused an approval-gated tool call ID"):
        await execute_mcp_approval_requests(
            agent=agent,
            approval_requests=approval_requests,
            context_wrapper=context_wrapper,
        )

    assert callback_calls == []
