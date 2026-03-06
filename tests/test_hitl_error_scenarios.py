"""Regression tests for HITL edge cases."""

from __future__ import annotations

from typing import Any, Callable, Optional, cast

import pytest
from openai.types.responses import ResponseComputerToolCall, ResponseFunctionToolCall
from openai.types.responses.response_computer_tool_call import ActionScreenshot
from openai.types.responses.response_input_param import (
    ComputerCallOutput,
    LocalShellCallOutput,
)
from openai.types.responses.response_output_item import LocalShellCall, McpApprovalRequest

from agents import (
    Agent,
    ApplyPatchTool,
    ComputerTool,
    LocalShellTool,
    Runner,
    RunResult,
    RunState,
    ShellTool,
    ToolApprovalItem,
    function_tool,
)
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
from agents.run_internal import run_loop
from agents.run_internal.run_loop import (
    NextStepInterruption,
    NextStepRunAgain,
    ProcessedResponse,
    ToolRunComputerAction,
    ToolRunFunction,
    ToolRunMCPApprovalRequest,
    ToolRunShellCall,
    extract_tool_call_id,
)
from agents.run_state import RunState as RunStateClass
from agents.tool import HostedMCPTool
from agents.usage import Usage

from .fake_model import FakeModel
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
    make_apply_patch_call,
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
    apply_patch_call = make_apply_patch_call("call_apply_1")

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
        raw_call=make_apply_patch_call("call_apply_pending"),
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
        raw_call = make_apply_patch_call("call_apply_auto")
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
async def test_nested_agent_tool_interruptions_dont_collide_on_duplicate_call_ids() -> None:
    """Nested agent tool interruptions should survive duplicate outer call IDs."""

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
                    agent_tool.name, call_id="outer-dup", arguments='{"input":"a"}'
                ),
                make_function_tool_call(
                    agent_tool.name, call_id="outer-dup", arguments='{"input":"b"}'
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
async def test_hosted_mcp_approval_matches_unknown_tool_key() -> None:
    """Approved hosted MCP interruptions should resume even when the tool name is missing."""
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
    context_wrapper.approve_tool(approval_item)

    class DummyMcpTool:
        on_approval_request: Any = None

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[
            ToolRunMCPApprovalRequest(
                request_item=McpApprovalRequest(
                    id="mcp-123",
                    type="mcp_approval_request",
                    server_label="test_server",
                    arguments="{}",
                    name="hosted_mcp",
                ),
                mcp_tool=cast(Any, DummyMcpTool()),
            )
        ],
        interruptions=[],
    )

    result = await run_loop.resolve_interrupted_turn(
        agent=agent,
        original_input="test",
        original_pre_step_items=[approval_item],
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
    ), "Approved hosted MCP call should emit an approval response"


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
        await run_loop.resolve_interrupted_turn(
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
        await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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
    pending_item = ToolApprovalItem(agent=agent, raw_item=pending_raw)
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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

    result = await run_loop.resolve_interrupted_turn(
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
