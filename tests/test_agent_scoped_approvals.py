from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from agents import Agent, RunConfig, RunContextWrapper, Runner, RunState, ShellTool, handoff
from agents.decorators import tool
from agents.items import ToolApprovalItem, ToolCallOutputItem
from agents.testing import ScriptedModel
from tests.model_test_helpers import get_exact_output_stream_step
from tests.test_responses import get_function_tool_call, get_text_message
from tests.utils.hitl import make_shell_call


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize("approve", [False, True])
async def test_sticky_approval_stays_with_agent_across_handoffs(
    streamed: bool, serialized: bool, approve: bool
) -> None:
    executed: list[str] = []
    policies: list[str] = []

    async def policy(_context: Any, _arguments: dict[str, Any], call_id: str) -> bool:
        policies.append(call_id)
        return True

    @tool(name_override="operate", needs_approval=policy)
    async def first_operation() -> str:
        executed.append("first")
        return "first"

    @tool(name_override="operate", needs_approval=policy)
    async def second_operation() -> str:
        executed.append("second")
        return "second"

    def model(steps: list[Any]) -> ScriptedModel:
        return ScriptedModel(
            steps=[get_exact_output_stream_step(step) for step in steps] if streamed else steps
        )

    first = Agent(
        name="same-name",
        tools=[first_operation],
        model=model(
            [
                [get_function_tool_call("operate", "{}", call_id="first-1")],
                [get_function_tool_call("to_second", "{}", call_id="handoff-1")],
                [get_function_tool_call("operate", "{}", call_id="first-2")],
                [get_text_message("done")],
            ]
        ),
    )
    second = Agent(
        name="same-name",
        tools=[second_operation],
        model=model(
            [
                [get_function_tool_call("operate", "{}", call_id="second-1")],
                [get_function_tool_call("operate", "{}", call_id="second-2")],
                [get_function_tool_call("to_first", "{}", call_id="handoff-2")],
            ]
        ),
    )
    first.handoffs = [handoff(second, tool_name_override="to_second")]
    second.handoffs = [handoff(first, tool_name_override="to_first")]
    config = RunConfig(tracing_disabled=True)

    async def run(value: Any) -> Any:
        if streamed:
            result = Runner.run_streamed(first, value, run_config=config)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(first, value, run_config=config)

    async def roundtrip(state: RunState[Any, Agent[Any]]) -> RunState[Any, Agent[Any]]:
        nonlocal first, second
        serialized_state = state.to_string()
        # Recreate the configured graph, retaining duplicate names and graph positions.
        first, second = first.clone(), second.clone()
        first.handoffs = [handoff(second, tool_name_override="to_second")]
        second.handoffs = [handoff(first, tool_name_override="to_first")]
        return await RunState.from_string(first, serialized_state)

    result = await run("start")
    state = result.to_state()
    if approve:
        state.approve(result.interruptions[0], always_approve=True)
    else:
        state.reject(result.interruptions[0], always_reject=True, rejection_message="first denied")
    if serialized:
        state = await roundtrip(state)
    result = await run(state)
    assert len(result.interruptions) == 1
    assert result.interruptions[0].agent is second
    assert policies == ["first-1", "second-1"]
    assert executed == (["first"] if approve else [])

    state = result.to_state()
    if approve:
        state.reject(result.interruptions[0], always_reject=True, rejection_message="second denied")
    else:
        state.approve(result.interruptions[0], always_approve=True)
    if serialized:
        state = await roundtrip(state)
    result = await run(state)
    assert result.final_output == "done"
    assert not result.interruptions
    assert policies == ["first-1", "second-1"]
    assert executed == (["first", "first"] if approve else ["second", "second"])
    rejected_agent = second if approve else first
    reason = "second denied" if approve else "first denied"
    rejections = [
        item.output
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem)
        and item.agent is rejected_agent
        and item.raw_item.get("call_id") in {"first-1", "first-2", "second-1", "second-2"}
    ]
    assert rejections == [reason, reason]


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [False, True])
async def test_legacy_sticky_decisions_require_approval_after_restore(approve: bool) -> None:
    executed: list[str] = []
    policies: list[str] = []

    async def policy(_context: Any, _arguments: dict[str, Any], call_id: str) -> bool:
        policies.append(call_id)
        return True

    @tool(needs_approval=policy)
    async def operate() -> str:
        executed.append("called")
        return "result"

    model = ScriptedModel(
        steps=[
            [get_function_tool_call("operate", "{}", call_id="pending")],
            [get_text_message("done")],
        ]
    )
    source = Agent(
        name="source",
        tools=[operate],
        model=ScriptedModel(
            steps=[[get_function_tool_call("transfer_to_destination", "{}", call_id="transfer")]]
        ),
    )
    destination = Agent(name="destination", tools=[operate], model=model)
    source.handoffs = [destination]
    initial = await Runner.run(source, "start", run_config=RunConfig(tracing_disabled=True))
    serialized = initial.to_state().to_json()
    serialized["$schemaVersion"] = "1.17"
    # The released format cannot tell which agent granted this permanent decision.
    serialized["context"]["approvals"] = {"operate": {"approved": approve, "rejected": not approve}}
    restored = await RunState.from_json(source, serialized)
    result = await Runner.run(source, restored, run_config=RunConfig(tracing_disabled=True))
    assert len(result.interruptions) == 1
    assert result.interruptions[0].agent is destination
    assert executed == []
    assert policies == ["pending"]
    resumed = result.to_state()
    resumed.approve(result.interruptions[0])
    result = await Runner.run(source, resumed, run_config=RunConfig(tracing_disabled=True))
    assert result.final_output == "done"
    assert executed == ["called"]


def test_name_only_query_does_not_choose_between_agent_owners() -> None:
    context = RunContextWrapper(context=None)
    first, second = Agent(name="same"), Agent(name="same")
    context.approve_tool(
        ToolApprovalItem(
            agent=first, raw_item=get_function_tool_call("operate", "{}", call_id="first")
        ),
        always_approve=True,
    )
    assert context.is_tool_approved("operate", "future") is True
    assert context.get_approval_status("operate", "future") is None
    context.reject_tool(
        ToolApprovalItem(
            agent=second, raw_item=get_function_tool_call("operate", "{}", call_id="second")
        ),
        always_reject=True,
        rejection_message="second denied",
    )
    assert context.is_tool_approved("operate", "future") is None
    assert context.get_rejection_message("operate", "future") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("native_sticky", [False, True])
async def test_function_sticky_rejection_does_not_replace_native_rejection_message(
    native_sticky: bool,
) -> None:
    @tool(name_override="shell", needs_approval=True)
    async def function_shell() -> str:
        raise AssertionError("Rejected function must not execute")

    def execute_shell(_request: Any) -> str:
        raise AssertionError("Rejected shell must not execute")

    destination = Agent(
        name="destination",
        tools=[ShellTool(executor=execute_shell, needs_approval=True)],
        model=ScriptedModel(
            steps=[
                [make_shell_call("native")],
                *([[make_shell_call("native-again")]] if native_sticky else []),
                [get_text_message("done")],
            ]
        ),
    )
    source = Agent(
        name="source",
        tools=[function_shell],
        handoffs=[destination],
        model=ScriptedModel(
            steps=[
                [get_function_tool_call("shell", "{}", call_id="function")],
                [get_function_tool_call("transfer_to_destination", "{}", call_id="transfer")],
            ]
        ),
    )
    config = RunConfig(tracing_disabled=True)
    result = await Runner.run(source, "start", run_config=config)
    state = result.to_state()
    state.reject(result.interruptions[0], always_reject=True, rejection_message="function denied")
    result = await Runner.run(source, state, run_config=config)
    assert len(result.interruptions) == 1
    assert result.interruptions[0].agent is destination
    state = result.to_state()
    state.reject(
        result.interruptions[0], always_reject=native_sticky, rejection_message="native denied"
    )
    result = await Runner.run(source, state, run_config=config)
    assert result.final_output == "done"
    outputs = [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert outputs == ["function denied", *(["native denied"] * (2 if native_sticky else 1))]


@pytest.mark.asyncio
async def test_function_exact_approval_preserves_native_sticky_rejection() -> None:
    @tool(name_override="shell", needs_approval=True)
    async def function_shell() -> str:
        return "function result"

    def execute_shell(_request: Any) -> str:
        raise AssertionError("Native sticky rejection must remain in force")

    first = Agent(
        name="native",
        tools=[ShellTool(executor=execute_shell, needs_approval=True)],
        model=ScriptedModel(
            steps=[
                [make_shell_call("native-1")],
                [get_function_tool_call("to_function", "{}", call_id="handoff-1")],
                [make_shell_call("native-2")],
                [get_text_message("done")],
            ]
        ),
    )
    second = Agent(
        name="function",
        tools=[function_shell],
        model=ScriptedModel(
            steps=[
                [get_function_tool_call("shell", "{}", call_id="function-1")],
                [get_function_tool_call("to_native", "{}", call_id="handoff-2")],
            ]
        ),
    )
    first.handoffs = [handoff(second, tool_name_override="to_function")]
    second.handoffs = [handoff(first, tool_name_override="to_native")]
    config = RunConfig(tracing_disabled=True)
    result = await Runner.run(first, "start", run_config=config)
    state = result.to_state()
    state.reject(result.interruptions[0], always_reject=True, rejection_message="native denied")
    result = await Runner.run(first, state, run_config=config)
    assert len(result.interruptions) == 1
    assert result.interruptions[0].agent is second
    state = result.to_state()
    state.approve(result.interruptions[0])
    result = await Runner.run(first, state, run_config=config)
    assert result.final_output == "done"
    assert [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)] == [
        "native denied",
        "function result",
        "native denied",
    ]


@pytest.mark.asyncio
async def test_function_decision_cannot_skip_sandbox_operation_approval() -> None:
    from pathlib import Path

    from agents.sandbox.capabilities.tools import SandboxApplyPatchTool
    from tests.sandbox._apply_patch_test_session import ApplyPatchSession

    session = ApplyPatchSession()
    protected = Path("/workspace/protected.txt")
    session.files[protected] = b"protected"
    checked: list[str] = []

    async def policy(_context: Any, operation: Any, _call_id: str) -> bool:
        checked.append(operation.type)
        return operation.type == "delete_file"

    patch_tool = SandboxApplyPatchTool(session=session, needs_approval=policy)
    patch = (
        "*** Begin Patch\n*** Add File: harmless.txt\n+hello\n"
        "*** Delete File: protected.txt\n*** End Patch\n"
    )
    destination = Agent(
        name="destination",
        tools=[patch_tool],
        model=ScriptedModel(
            steps=[
                [
                    {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": "native",
                        "input": patch,
                    }
                ],
                [get_text_message("done")],
            ]
        ),
    )

    @tool(name_override="apply_patch", needs_approval=True)
    async def source_tool() -> str:
        raise AssertionError("Rejected function must not execute")

    source = Agent(
        name="source",
        tools=[source_tool],
        handoffs=[destination],
        model=ScriptedModel(
            steps=[
                [get_function_tool_call("apply_patch", "{}", call_id="function")],
                [get_function_tool_call("transfer_to_destination", "{}", call_id="handoff")],
            ]
        ),
    )
    config = RunConfig(tracing_disabled=True)
    result = await Runner.run(source, "start", run_config=config)
    state = result.to_state()
    state.reject(result.interruptions[0], always_reject=True)
    result = await Runner.run(source, state, run_config=config)
    assert len(result.interruptions) == 1
    assert result.interruptions[0].agent is destination
    assert checked == ["create_file", "delete_file"]
    assert session.files == {protected: b"protected"}


def _convert_approval_context(
    context: RunContextWrapper[dict[str, str]], converter: str
) -> RunContextWrapper[dict[str, str]]:
    adapter = TypeAdapter(RunContextWrapper[dict[str, str]])
    assert adapter.json_schema()["type"] == "object"
    if converter == "python":
        return adapter.validate_python(adapter.dump_python(context))
    if converter == "json":
        payload = adapter.dump_json(context)
        assert b"private-agent-instructions" not in payload
        return adapter.validate_json(payload)
    temporal = pytest.importorskip("temporalio.contrib.pydantic")
    codec = temporal.PydanticJSONPlainPayloadConverter()
    payload = codec.to_payload(context)
    assert b"private-agent-instructions" not in payload.data
    return codec.from_payload(payload, RunContextWrapper[dict[str, str]])


@pytest.mark.parametrize("converter", ["python", "json", "temporal"])
@pytest.mark.parametrize("decision", ["approve", "reject", "always_approve", "always_reject"])
def test_context_converters_preserve_function_decisions(converter: str, decision: str) -> None:
    @tool
    def operate() -> str:
        return "done"

    agent = Agent(name="same-name", instructions="private-agent-instructions", tools=[operate])
    approval = ToolApprovalItem(
        agent=agent, raw_item=get_function_tool_call("operate", "{}", call_id="first")
    )
    context = RunContextWrapper(context={"tenant": "synthetic"})
    approved = decision.endswith("approve")
    always = decision.startswith("always_")
    if approved:
        context.approve_tool(approval, always_approve=always)
    else:
        context.reject_tool(approval, always_reject=always, rejection_message="Owner rejected")
    native = ToolApprovalItem(agent=agent, raw_item=make_shell_call(call_id="native"))
    context.reject_tool(native, rejection_message="Native rejected")

    # Repeated transport must preserve decisions without leaking the agent graph.
    restored = _convert_approval_context(_convert_approval_context(context, converter), converter)
    assert restored.context == {"tenant": "synthetic"}
    assert restored.is_tool_approved("operate", "first") is approved
    assert restored.is_tool_approved("operate", "next") is (approved if always else None)
    assert restored.get_rejection_message("operate", "first") == (
        None if approved else "Owner rejected"
    )
    assert restored.get_approval_status("shell", "native", existing_pending=native) is False
    assert restored.get_rejection_message("shell", "native", existing_pending=native) == (
        "Native rejected"
    )
    assert context.get_approval_status("operate", "first", current_invocation=approval) is approved


@pytest.mark.parametrize("converter", ["python", "json", "temporal"])
def test_context_converters_preserve_distinct_same_named_owners(converter: str) -> None:
    context = RunContextWrapper(context={"tenant": "synthetic"})
    for index, agent in enumerate([Agent(name="same-name"), Agent(name="same-name")]):
        approval = ToolApprovalItem(
            agent=agent, raw_item=get_function_tool_call("operate", "{}", call_id=f"call-{index}")
        )
        if index == 0:
            context.approve_tool(approval, always_approve=True)
        else:
            context.reject_tool(approval, always_reject=True, rejection_message="Second owner")
    restored = _convert_approval_context(_convert_approval_context(context, converter), converter)
    # Name-only inspection cannot choose between owners, even after transport.
    assert restored.is_tool_approved("operate", "call-0") is None
    assert restored.get_rejection_message("operate", "call-1") is None
    assert len(restored._approvals) == 2


@pytest.mark.parametrize("always", [False, True])
@pytest.mark.parametrize("approve", [False, True])
def test_python_context_conversion_preserves_mixed_hosted_decisions(
    always: bool, approve: bool
) -> None:
    from tests.test_run_context_approvals import _make_hosted_mcp_approval_item

    agent = Agent(name="owner")
    context = RunContextWrapper(context={"tenant": "synthetic"})
    function = ToolApprovalItem(
        agent=agent, raw_item=get_function_tool_call("operate", "{}", call_id="function")
    )
    context.approve_tool(function, always_approve=True)
    hosted = _make_hosted_mcp_approval_item(agent, request_id="hosted", server_label="server")
    if approve:
        context.approve_tool(hosted, always_approve=always)
    else:
        context.reject_tool(hosted, always_reject=always, rejection_message="Hosted rejected")

    restored = _convert_approval_context(_convert_approval_context(context, "python"), "python")
    assert restored.is_tool_approved("operate", "function") is True
    assert restored.is_tool_approved("lookup_account", "hosted") is approve
    assert (
        restored.get_approval_status("lookup_account", "hosted", existing_pending=hosted) is approve
    )
    assert restored.get_rejection_message("lookup_account", "hosted", existing_pending=hosted) == (
        None if approve else "Hosted rejected"
    )
    next_call = _make_hosted_mcp_approval_item(agent, request_id="next", server_label="server")
    other_server = _make_hosted_mcp_approval_item(agent, request_id="other", server_label="other")
    assert restored.get_approval_status("lookup_account", "next", existing_pending=next_call) is (
        approve if always else None
    )
    assert (
        restored.get_approval_status("lookup_account", "other", existing_pending=other_server)
        is None
    )


@pytest.mark.parametrize("converter", ["none", "python", "json", "temporal"])
def test_name_only_inspection_selects_a_unique_exact_call(converter: str) -> None:
    first, second = Agent(name="same"), Agent(name="same")
    context = RunContextWrapper(context={"tenant": "synthetic"})
    approved = ToolApprovalItem(
        agent=first, raw_item=get_function_tool_call("operate", "{}", call_id="call-a")
    )
    rejected = ToolApprovalItem(
        agent=second, raw_item=get_function_tool_call("operate", "{}", call_id="call-b")
    )
    context.approve_tool(approved)
    context.reject_tool(rejected, rejection_message="Second call denied")

    def inspect() -> RunContextWrapper[dict[str, str]]:
        return context if converter == "none" else _convert_approval_context(context, converter)

    observed = inspect()
    assert observed.is_tool_approved("operate", "call-a") is True
    assert observed.is_tool_approved("operate", "call-b") is False
    assert observed.get_rejection_message("operate", "call-a") is None
    assert observed.get_rejection_message("operate", "call-b") == "Second call denied"
    assert observed.is_tool_approved("operate", "future") is None
    # Inspection must not become an execution grant for the other owner.
    other_owner = ToolApprovalItem(agent=second, raw_item=approved.raw_item)
    assert observed.get_approval_status("operate", "call-a", current_invocation=other_owner) is None

    context.approve_tool(rejected, always_approve=True)
    context.reject_tool(approved, rejection_message="First call denied")
    observed = inspect()
    assert observed.is_tool_approved("operate", "call-a") is False
    assert observed.get_rejection_message("operate", "call-a") == "First call denied"
    # A unique exact decision wins inspection over another owner's sticky default.
    assert observed.is_tool_approved("operate", "future") is None
    assert observed.get_rejection_message("operate", "future") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("converter", ["python", "json", "temporal"])
@pytest.mark.parametrize("decision", ["approve", "reject", "always_approve", "always_reject"])
@pytest.mark.parametrize("streamed", [False, True])
async def test_runner_executes_converted_context_decisions(
    converter: str, decision: str, streamed: bool
) -> None:
    executed: list[str] = []

    @tool(needs_approval=True)
    def operate() -> str:
        executed.append("executed")
        return "result"

    source = Agent(name="owner", tools=[operate])
    call = get_function_tool_call("operate", "{}", call_id="first")
    approval = ToolApprovalItem(agent=source, raw_item=call)
    context = RunContextWrapper(context={"tenant": "synthetic"})
    approved = decision.endswith("approve")
    always = decision.startswith("always_")
    if approved:
        context.approve_tool(approval, always_approve=always)
    else:
        context.reject_tool(approval, always_reject=always, rejection_message="Owner rejected")
    restored = _convert_approval_context(_convert_approval_context(context, converter), converter)
    steps: list[Any] = [[call]]
    if always:
        steps.append([get_function_tool_call("operate", "{}", call_id="next")])
    steps.append([get_text_message("done")])
    configured = source
    configured.model = ScriptedModel(
        steps=[get_exact_output_stream_step(step) for step in steps] if streamed else steps
    )
    config = RunConfig(tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(configured, "start", context=restored, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(configured, "start", context=restored, run_config=config)
    assert not result.interruptions
    assert result.final_output == "done"
    call_count = 2 if always else 1
    assert executed == (["executed"] * call_count if approved else [])
    outputs = [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert outputs == (["result"] if approved else ["Owner rejected"]) * call_count
    # Executable rebinding must also leave a graph-serializable checkpoint.
    await RunState.from_string(configured, result.to_state().to_string())


@pytest.mark.asyncio
@pytest.mark.parametrize("converter", ["python", "json", "temporal"])
@pytest.mark.parametrize("always", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
async def test_converted_context_does_not_authorize_same_named_distinct_capability(
    converter: str, always: bool, streamed: bool
) -> None:
    executed: list[str] = []

    @tool(name_override="operate", needs_approval=True)
    def low_privilege() -> str:
        return "public data"

    @tool(name_override="operate", needs_approval=True)
    def privileged() -> str:
        executed.append("privileged")
        return "private data"

    source = Agent(name="owner", tools=[low_privilege])
    call = get_function_tool_call("operate", "{}", call_id="approved")
    context = RunContextWrapper(context={"tenant": "synthetic"})
    context.approve_tool(ToolApprovalItem(agent=source, raw_item=call), always_approve=always)
    restored = _convert_approval_context(_convert_approval_context(context, converter), converter)
    requested = get_function_tool_call("operate", "{}", call_id="next") if always else call
    steps = [[requested], [get_text_message("done")]]
    configured = Agent(
        name="owner",
        tools=[privileged],
        model=ScriptedModel(
            steps=[get_exact_output_stream_step(step) for step in steps] if streamed else steps
        ),
    )
    config = RunConfig(tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(configured, "start", context=restored, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(configured, "start", context=restored, run_config=config)
    assert executed == []
    assert len(result.interruptions) == 1
    assert result.interruptions[0].agent is configured
    assert not [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    state = result.to_state()
    state.approve(result.interruptions[0])
    if streamed:
        resumed = Runner.run_streamed(configured, state, run_config=config)
        async for _ in resumed.stream_events():
            pass
    else:
        resumed = await Runner.run(configured, state, run_config=config)
    assert resumed.final_output == "done"
    assert executed == ["privileged"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguity", ["target", "source", "missing", "clone", "copy"])
async def test_runner_requires_reapproval_for_unresolved_context_owners(ambiguity: str) -> None:
    @tool(needs_approval=True)
    def operate() -> str:
        raise AssertionError("Unresolved decisions must not execute")

    source = Agent(name="owner", tools=[operate])
    call = get_function_tool_call("operate", "{}", call_id="first")
    context = RunContextWrapper(context={"tenant": "synthetic"})
    context.approve_tool(ToolApprovalItem(agent=source, raw_item=call), always_approve=True)
    if ambiguity == "source":
        sibling = source.clone()
        context.approve_tool(
            ToolApprovalItem(
                agent=sibling, raw_item=get_function_tool_call("operate", "{}", call_id="other")
            ),
            always_approve=True,
        )
    configured = source.clone(
        name="different" if ambiguity == "missing" else "owner",
        model=ScriptedModel(steps=[[call]]),
    )
    if ambiguity == "copy":
        import copy

        # A shallow copy carries the original cache, but not its owner witness.
        configured = copy.copy(source)
        configured.model = ScriptedModel(steps=[[call]])
    if ambiguity == "target":
        configured.handoffs = [source]
    restored = _convert_approval_context(context, "json")
    result = await Runner.run(
        configured, "start", context=restored, run_config=RunConfig(tracing_disabled=True)
    )
    assert len(result.interruptions) == 1
    assert result.interruptions[0].agent is configured
    state = result.to_state()
    state.reject(result.interruptions[0], rejection_message="Fresh decision")
    await RunState.from_string(configured, state.to_string())


@pytest.mark.asyncio
@pytest.mark.parametrize("converter", ["python", "json", "temporal"])
async def test_converted_context_preserves_approved_invocation(converter: str) -> None:
    from agents import ModelBehaviorError

    executed: list[str] = []

    @tool(needs_approval=True)
    def operate(value: str) -> str:
        executed.append(value)
        return value

    source = Agent(name="owner", tools=[operate])
    approved = get_function_tool_call("operate", '{"value":"approved"}', call_id="same")
    context = RunContextWrapper(context={"tenant": "synthetic"})
    context.approve_tool(ToolApprovalItem(agent=source, raw_item=approved))
    restored = _convert_approval_context(context, converter)
    configured = source
    configured.model = ScriptedModel(
        steps=[
            [get_function_tool_call("operate", '{"value":"changed"}', call_id="same")],
            [get_text_message("done")],
        ]
    )
    with pytest.raises(ModelBehaviorError, match="reused a tool call ID"):
        await Runner.run(
            configured, "start", context=restored, run_config=RunConfig(tracing_disabled=True)
        )
    assert executed == []


@pytest.mark.asyncio
async def test_reused_context_snapshots_only_current_graph_approvals() -> None:
    @tool(needs_approval=True)
    def operate() -> str:
        return "done"

    first_call = get_function_tool_call("operate", "{}", call_id="first")
    first = Agent(
        name="first",
        tools=[operate],
        model=ScriptedModel(steps=[[first_call], [get_text_message("done")]]),
    )
    context = RunContextWrapper(context={"tenant": "synthetic"})
    config = RunConfig(tracing_disabled=True)
    pending = await Runner.run(first, "start", context=context, run_config=config)
    context.approve_tool(pending.interruptions[0], always_approve=True)
    second = Agent(
        name="second",
        tools=[operate],
        model=ScriptedModel(steps=[[get_function_tool_call("operate", "{}", call_id="second")]]),
    )
    result = await Runner.run(second, "start", context=context, run_config=config)
    assert len(result.interruptions) == 1
    state = result.to_state()
    state.reject(result.interruptions[0], rejection_message="Second denied")
    restored = await RunState.from_string(second, state.to_string())
    assert (
        cast(RunContextWrapper[dict[str, str]], restored._context).get_rejection_message(
            "operate", "second"
        )
        == "Second denied"
    )
    # Snapshot projection does not revoke the reusable wrapper's other owner.
    future = ToolApprovalItem(
        agent=first, raw_item=get_function_tool_call("operate", "{}", call_id="future")
    )
    assert context.get_approval_status("operate", "future", current_invocation=future) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("converter", ["python", "json", "temporal"])
async def test_converted_context_does_not_repeat_completed_call(converter: str) -> None:
    executed: list[str] = []

    @tool(needs_approval=True)
    def operate() -> str:
        executed.append("executed")
        return "result"

    call = get_function_tool_call("operate", "{}", call_id="same")
    agent = Agent(
        name="owner",
        tools=[operate],
        model=ScriptedModel(steps=[[call], [get_text_message("done")]]),
    )
    context = RunContextWrapper(context={"tenant": "synthetic"})
    context.approve_tool(ToolApprovalItem(agent=agent, raw_item=call), always_approve=True)
    config = RunConfig(tracing_disabled=True)
    await Runner.run(agent, "start", context=context, run_config=config)
    restored = _convert_approval_context(context, converter)
    configured = agent
    configured.model = ScriptedModel(steps=[[call], [get_text_message("done")]])
    result = await Runner.run(configured, "start", context=restored, run_config=config)
    assert result.final_output == "done"
    assert not result.interruptions
    assert executed == ["executed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("converter", ["python", "json", "temporal"])
@pytest.mark.parametrize("decision", ["approve", "reject", "always_approve", "always_reject"])
async def test_converted_context_retains_other_graph_decisions(
    converter: str, decision: str
) -> None:
    executed: list[str] = []

    @tool(needs_approval=True)
    def operate() -> str:
        executed.append("executed")
        return "result"

    context = RunContextWrapper(context={"tenant": "synthetic"})
    agents = [Agent(name="owner", tools=[operate]) for _ in range(2)]
    calls = [get_function_tool_call("operate", "{}", call_id=name) for name in ["first", "second"]]
    approved = decision.endswith("approve")
    for agent, call in zip(agents, calls, strict=False):
        item = ToolApprovalItem(agent=agent, raw_item=call)
        if approved:
            context.approve_tool(item, always_approve=decision.startswith("always_"))
        else:
            context.reject_tool(
                item, always_reject=decision.startswith("always_"), rejection_message="Denied"
            )
    restored = _convert_approval_context(context, converter)
    for agent, call in zip(agents, calls, strict=False):
        configured = agent
        configured.model = ScriptedModel(steps=[[call], [get_text_message("done")]])
        result = await Runner.run(
            configured, "start", context=restored, run_config=RunConfig(tracing_disabled=True)
        )
        assert not result.interruptions
        assert result.final_output == "done"
        assert [
            item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
        ] == ["result" if approved else "Denied"]
    assert executed == (["executed", "executed"] if approved else [])


@pytest.mark.parametrize("approve", [False, True])
def test_approval_context_does_not_retain_discarded_agent_graphs(approve: bool) -> None:
    import gc
    import weakref

    context = RunContextWrapper(context={"tenant": "synthetic"})
    references = []
    for index in range(10):
        child = Agent(name="child")
        agent = Agent(name="owner", handoffs=[child])
        references.extend([weakref.ref(agent), weakref.ref(child)])
        item = ToolApprovalItem(
            agent=agent, raw_item=get_function_tool_call("operate", "{}", call_id=str(index))
        )
        if approve:
            context.approve_tool(item)
        else:
            context.reject_tool(item, rejection_message="Denied")
        del item, agent, child
    gc.collect()
    assert all(reference() is None for reference in references)
    # Compact decisions remain inspectable without retaining application graphs.
    assert len(context._approvals) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("tied_at_write", [False, True])
@pytest.mark.parametrize("always", [False, True])
async def test_tied_duplicate_owner_restore_requires_reapproval(
    tied_at_write: bool, always: bool
) -> None:
    executed: list[str] = []
    call = get_function_tool_call("operate", "{}", call_id="pending")

    def make_agent(label: str, instructions: str) -> Agent[Any]:
        @tool(name_override="operate", needs_approval=True)
        def operation() -> str:
            executed.append(label)
            return label

        return Agent(
            name="same",
            instructions=instructions,
            tools=[operation],
            model=ScriptedModel(steps=[[call], [get_text_message("done")]]),
        )

    first = make_agent("first", "same" if tied_at_write else "first")
    second = make_agent("second", "same" if tied_at_write else "second")
    root = Agent(
        name="root",
        handoffs=[
            handoff(first, tool_name_override="to_first"),
            handoff(second, tool_name_override="to_second"),
        ],
        model=ScriptedModel(steps=[[get_function_tool_call("to_first", "{}", call_id="handoff")]]),
    )
    config = RunConfig(tracing_disabled=True)
    result = await Runner.run(root, "start", run_config=config)
    assert result.interruptions[0].agent is first
    state = result.to_state()
    state.approve(result.interruptions[0], always_approve=always)
    payload = state.to_string()
    # Identical visible signatures cannot distinguish these different closures.
    restored_first = make_agent("restored-first", "same")
    restored_second = make_agent("restored-second", "same")
    configured = root.clone(
        handoffs=[
            handoff(restored_second, tool_name_override="to_second"),
            handoff(restored_first, tool_name_override="to_first"),
        ]
    )
    restored = await RunState.from_string(configured, payload)
    resumed = await Runner.run(configured, restored, run_config=config)
    assert len(resumed.interruptions) == 1
    assert executed == []
