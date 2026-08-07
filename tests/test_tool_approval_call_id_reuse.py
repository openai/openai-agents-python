from __future__ import annotations

import asyncio
import json
import warnings
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from openai.types.responses import ResponseCustomToolCall, ResponseFunctionToolCall
from openai.types.responses.response_computer_tool_call import (
    ActionScreenshot,
    PendingSafetyCheck,
    ResponseComputerToolCall,
)
from openai.types.responses.response_function_web_search import (
    ActionSearch,
    ActionSearchSource,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_output_item import McpApprovalRequest
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from agents import (
    Agent,
    ApplyPatchTool,
    ComputerTool,
    CustomTool,
    HostedMCPTool,
    MCPToolApprovalFunctionResult,
    MCPToolApprovalRequest,
    RunConfig,
    Runner,
    ShellTool,
    ToolGuardrailFunctionOutput,
    ToolOutputGuardrailData,
    ToolOutputGuardrailTripwireTriggered,
    function_tool,
    handoff,
    tool_output_guardrail,
)
from agents._tool_invocation import (
    tool_invocation_call_id,
    tool_invocation_identity,
    tool_invocation_identity_and_scope,
)
from agents.editor import ApplyPatchOperation, ApplyPatchResult
from agents.exceptions import ModelBehaviorError, UserError
from agents.items import ModelResponse, ToolApprovalItem
from agents.lifecycle import RunHooks
from agents.models.interface import Model, ModelProvider
from agents.run_context import RunContextWrapper
from agents.run_internal.run_loop import ToolRunFunction
from agents.run_internal.tool_execution import (
    collect_manual_mcp_approvals,
    process_hosted_mcp_approvals,
    resolve_approval_rejection_message,
)
from agents.run_internal.tool_planning import _collect_runs_by_approval
from agents.run_state import RunState
from agents.stream_events import RunItemStreamEvent
from agents.tool import Tool
from agents.tool_context import ToolContext
from tests.fake_model import FakeModel
from tests.test_computer_tool_lifecycle import FakeComputer
from tests.test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message
from tests.utils.hitl import make_apply_patch_dict, make_shell_call, make_state_with_interruptions


class _ScriptedProvider(ModelProvider):
    def __init__(self, model: Model) -> None:
        self.model = model

    def get_model(self, model_name: str | None) -> Model:
        assert model_name == "scripted-provider-model"
        return self.model


def test_canonical_shell_identity_ignores_stripped_provider_metadata() -> None:
    provider_call = {
        "type": "shell_call",
        "call_id": "shell_0",
        "action": {"commands": ["echo safe"]},
        "created_by": "server",
    }
    persisted_call = dict(provider_call)
    persisted_call.pop("created_by")

    assert tool_invocation_identity(provider_call) == tool_invocation_identity(persisted_call)


def test_web_search_source_schema_drift_does_not_warn_during_invocation_lookup() -> None:
    source = ActionSearchSource.model_construct(type="api", name="oai-calculator")
    output_item = ResponseFunctionWebSearch(
        id="ws_123",
        action=ActionSearch(type="search", query="current market data", sources=[source]),
        status="completed",
        type="web_search_call",
    )

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always", UserWarning)
        call_id = tool_invocation_call_id(output_item)

    serializer_warnings = [
        warning
        for warning in caught_warnings
        if "Pydantic serializer warnings" in str(warning.message)
    ]
    assert call_id is None
    assert not serializer_warnings


def test_invocation_lookup_preserves_legacy_model_dump_signature() -> None:
    class LegacyModel:
        def model_dump(self, *, exclude_none: bool, exclude_unset: bool) -> dict[str, str]:
            assert exclude_none is True
            assert exclude_unset is True
            return {"type": "function_call", "call_id": "call_legacy"}

    assert tool_invocation_call_id(LegacyModel()) == ("function_call", "call_legacy")


def test_invocation_lookup_propagates_internal_model_dump_type_error() -> None:
    class FailingModel:
        def __init__(self) -> None:
            self.calls = 0

        def model_dump(
            self,
            *,
            exclude_none: bool,
            exclude_unset: bool,
            warnings: bool = True,
        ) -> dict[str, str]:
            self.calls += 1
            raise TypeError("internal serialization failure")

    model = FailingModel()
    with pytest.raises(TypeError, match="internal serialization failure"):
        tool_invocation_call_id(model)
    assert model.calls == 1


def test_canonical_shell_identity_treats_optional_nulls_as_omitted() -> None:
    provider_call = {
        "type": "shell_call",
        "call_id": "shell_0",
        "action": {
            "commands": ["echo safe"],
            "max_output_length": None,
            "timeout_ms": None,
        },
        "environment": None,
    }
    normalized_call = dict(provider_call)
    normalized_call["action"] = {"commands": ["echo safe"]}
    normalized_call.pop("environment")

    assert tool_invocation_identity(provider_call) == tool_invocation_identity(normalized_call)


def test_canonical_function_identity_preserves_null_argument_values() -> None:
    call_with_null = {
        "type": "function_call",
        "call_id": "call_0",
        "name": "lookup",
        "arguments": '{"value": null}',
    }
    call_without_value = {
        "type": "function_call",
        "call_id": "call_0",
        "name": "lookup",
        "arguments": "{}",
    }

    assert tool_invocation_identity(call_with_null) != tool_invocation_identity(call_without_value)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_type", ["custom", "function"])
async def test_completed_apply_patch_fallback_run_state_round_trip(fallback_type: str) -> None:
    class RecordingEditor:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
            self.paths.append(operation.path)
            return ApplyPatchResult(status="completed", output="updated")

    editor = RecordingEditor()
    tool = ApplyPatchTool(editor=cast(Any, editor))
    operation = {"type": "update_file", "path": "safe.txt", "diff": "-old\n+new\n"}
    if fallback_type == "custom":
        call: Any = ResponseCustomToolCall(
            type="custom_tool_call",
            name="apply_patch",
            call_id="patch_0",
            input=json.dumps(operation),
        )
    else:
        call = ResponseFunctionToolCall(
            type="function_call",
            name="apply_patch",
            call_id="patch_0",
            arguments=json.dumps(operation),
        )
    model = FakeModel(initial_output=[call])
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[tool])

    result = await Runner.run(agent, "update the file")
    restored = await RunState.from_json(agent, result.to_state().to_json())

    assert result.final_output == "done"
    assert editor.paths == ["safe.txt"]
    assert restored._context is not None
    assert restored._context._tool_invocations["patch_0"].completed is True


@pytest.mark.asyncio
async def test_streamed_function_apply_patch_replay_emits_one_tool_called_event() -> None:
    class RecordingEditor:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def update_file(self, operation: ApplyPatchOperation) -> ApplyPatchResult:
            self.paths.append(operation.path)
            return ApplyPatchResult(status="completed", output="updated")

    editor = RecordingEditor()
    operation = {"type": "update_file", "path": "safe.txt", "diff": "-old\n+new\n"}
    call = ResponseFunctionToolCall(
        type="function_call",
        name="apply_patch",
        call_id="patch_0",
        arguments=json.dumps(operation),
    )
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [[call], [call.model_copy(deep=True)], [get_text_message("done")]]
    )
    agent = Agent(
        name="agent",
        model=model,
        tools=[ApplyPatchTool(editor=cast(Any, editor))],
    )

    streamed = Runner.run_streamed(agent, "update the file")
    events = [event async for event in streamed.stream_events()]

    tool_called_events = [
        event
        for event in events
        if isinstance(event, RunItemStreamEvent) and event.name == "tool_called"
    ]
    assert streamed.final_output == "done"
    assert editor.paths == ["safe.txt"]
    assert len(tool_called_events) == 1
    assert tool_called_events[0].item.call_id == "patch_0"


@pytest.mark.parametrize(
    ("raw_item", "first_tool_name", "replacement_tool_name"),
    [
        (make_shell_call("call_0", commands=["echo safe"]), "safe_shell", "other_shell"),
        (make_apply_patch_dict("call_0"), "safe_patch", "other_patch"),
    ],
    ids=["shell", "apply_patch"],
)
def test_native_tool_approval_scope_rejects_resolved_tool_replacement(
    raw_item: Any,
    first_tool_name: str,
    replacement_tool_name: str,
) -> None:
    context: RunContextWrapper[Any] = RunContextWrapper(context=None)
    approval_item = ToolApprovalItem(
        agent=Agent(name="agent"),
        raw_item=cast(Any, raw_item),
        tool_name=first_tool_name,
    )
    context.approve_tool(approval_item)

    assert (
        context._approved_tool_invocation_status(  # noqa: SLF001
            raw_item,
            tool_name=first_tool_name,
        )
        is not None
    )
    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        context._approved_tool_invocation_status(  # noqa: SLF001
            raw_item,
            tool_name=replacement_tool_name,
        )


@pytest.mark.asyncio
async def test_custom_named_shell_sticky_approval_applies_to_fresh_call_ids() -> None:
    executed: list[str] = []

    def execute(request: Any) -> str:
        executed.extend(request.data.action.commands)
        return "ok"

    first_call = make_shell_call("call_0", commands=["echo first"])
    second_call = make_shell_call("call_1", commands=["echo second"])
    model = FakeModel()
    model.add_multiple_turn_outputs([[first_call], [second_call], [get_text_message("done")]])
    tool = ShellTool(
        executor=execute,
        name="safe_shell",
        needs_approval=True,
    )
    agent = Agent(name="agent", model=model, tools=[tool])

    first = await Runner.run(agent, "run commands")
    state = first.to_state()
    state.approve(first.interruptions[0], always_approve=True)
    resumed = await Runner.run(agent, state)

    assert resumed.final_output == "done"
    assert executed == ["echo first", "echo second"]


@pytest.mark.asyncio
async def test_custom_named_shell_replacement_rejects_completed_call_id_replay() -> None:
    first_executions: list[str] = []
    replacement_executions: list[str] = []

    def run_first(_request: Any) -> str:
        first_executions.append("ran")
        return "ok"

    def run_replacement(_request: Any) -> str:
        replacement_executions.append("ran")
        return "ok"

    call = make_shell_call("call_0", commands=["echo safe"])
    model = FakeModel(initial_output=[call])
    original_tool = ShellTool(
        executor=run_first,
        name="safe_shell",
        needs_approval=True,
    )
    agent = Agent(name="agent", model=model, tools=[original_tool])

    first = await Runner.run(agent, "run command")
    state = first.to_state()
    state.approve(first.interruptions[0])
    model.set_next_output([get_text_message("done")])
    completed = await Runner.run(agent, state)

    agent.tools = [
        ShellTool(
            executor=run_replacement,
            name="other_shell",
            needs_approval=False,
        )
    ]
    model.set_next_output([cast(Any, dict(cast(dict[str, Any], call)))])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, completed.to_state())

    assert first_executions == ["ran"]
    assert replacement_executions == []


@pytest.mark.asyncio
async def test_schema_1_13_custom_named_shell_skips_exact_completed_replay() -> None:
    executions: list[str] = []

    def execute(request: Any) -> str:
        executions.append(request.data.call_id)
        return "ok"

    call = make_shell_call("legacy-shell", commands=["echo safe"])
    model = FakeModel(initial_output=[call])
    model.set_next_output([get_text_message("done")])
    agent = Agent(
        name="agent",
        model=model,
        tools=[ShellTool(executor=execute, name="safe_shell")],
    )

    completed = await Runner.run(agent, "run")
    serialized = completed.to_state().to_json()
    serialized["$schemaVersion"] = "1.13"
    serialized["context"].pop("tool_invocations", None)
    for key in ("generated_items", "session_items"):
        for item in serialized.get(key, []):
            item.pop("tool_name", None)
    for item in (serialized.get("last_processed_response") or {}).get("new_items", []):
        item.pop("tool_name", None)

    restored = await RunState.from_json(agent, serialized)
    assert restored._context is not None
    expected_identity = tool_invocation_identity_and_scope(call, tool_name="safe_shell")
    assert expected_identity is not None
    restored_record = restored._context._tool_invocations["legacy-shell"]
    assert restored_record.approval_scope == expected_identity[2]
    assert restored_record.fingerprint == expected_identity[3]
    model.add_multiple_turn_outputs(
        [
            [call],
            [get_text_message("done again")],
        ]
    )
    resumed = await Runner.run(agent, restored)

    assert resumed.final_output == "done again"
    assert executions == ["legacy-shell"]


@pytest.mark.parametrize(
    "raw_item",
    [
        {"type": "custom_tool_call", "name": "raw_editor", "call_id": "call_0"},
        {"type": "computer_call", "call_id": "call_0"},
        {"type": "local_shell_call", "call_id": "call_0"},
        {"type": "shell_call", "call_id": "call_0"},
        {"type": "apply_patch_call", "call_id": "call_0"},
    ],
)
def test_canonical_identity_requires_each_tool_payload(raw_item: dict[str, Any]) -> None:
    assert tool_invocation_identity(raw_item) is None


@pytest.mark.asyncio
async def test_cancelled_rejection_formatter_leaves_invocation_executed() -> None:
    tool_call = {
        "type": "function_call",
        "name": "approval_tool",
        "call_id": "call_rejected",
        "arguments": "{}",
    }
    context: RunContextWrapper[Any] = RunContextWrapper(context=None)
    assert context._tool_invocation_status(tool_call) == (  # noqa: SLF001
        ("function_call", "call_rejected"),
        False,
        False,
    )
    formatter_entered = asyncio.Event()

    async def blocking_formatter(_args: Any) -> str:
        formatter_entered.set()
        await asyncio.Event().wait()
        return "rejected"

    task = asyncio.create_task(
        resolve_approval_rejection_message(
            context_wrapper=context,
            run_config=RunConfig(tool_error_formatter=blocking_formatter),
            tool_call=tool_call,
            tool_type="function",
            tool_name="approval_tool",
            call_id="call_rejected",
        )
    )
    await formatter_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert context._tool_invocation_status(tool_call) == (  # noqa: SLF001
        ("function_call", "call_rejected"),
        False,
        True,
    )


async def _run(
    agent: Agent[Any],
    input_value: Any,
    *,
    run_config: RunConfig,
    mode: Literal["non_streamed", "streamed"],
    hooks: RunHooks[Any] | None = None,
    events: list[Any] | None = None,
) -> Any:
    if mode == "non_streamed":
        return await Runner.run(agent, input_value, run_config=run_config, hooks=hooks)
    result = Runner.run_streamed(agent, input_value, run_config=run_config, hooks=hooks)
    async for event in result.stream_events():
        if events is not None:
            events.append(event)
    return result


def _build_scenario(
    second_call_id: str,
    second_value: str,
) -> tuple[Agent[Any], RunConfig, list[str]]:
    executed: list[str] = []

    @function_tool(name_override="record_value", needs_approval=True)
    def record_value(value: str) -> str:
        executed.append(value)
        return value

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "record_value",
                    json.dumps({"value": "safe"}),
                    call_id="call_0",
                )
            ],
            [
                get_function_tool_call(
                    "record_value",
                    json.dumps({"value": second_value}),
                    call_id=second_call_id,
                )
            ],
            [get_text_message("done")],
        ]
    )
    provider = _ScriptedProvider(model)
    agent = Agent(
        name="custom-provider-agent",
        model="scripted-provider-model",
        tools=[record_value],
    )
    return agent, RunConfig(model_provider=provider), executed


@pytest.mark.asyncio
async def test_empty_custom_tool_call_id_fails_before_approval_or_execution() -> None:
    callbacks: list[str] = []
    executed: list[str] = []

    async def approve(_context: RunContextWrapper[Any], _item: ToolApprovalItem) -> Any:
        callbacks.append("approval")
        return {"approve": True}

    async def invoke(_context: Any, raw_input: str) -> str:
        executed.append(raw_input)
        return raw_input

    tool = CustomTool(
        name="raw_editor",
        description="Edit raw text.",
        on_invoke_tool=invoke,
        format={"type": "text"},
        needs_approval=True,
        on_approval=approve,
    )
    model = FakeModel(
        initial_output=[
            ResponseCustomToolCall(
                type="custom_tool_call",
                name=tool.name,
                call_id="",
                input="changed",
            )
        ]
    )
    agent = Agent(name="agent", model=model, tools=[tool])

    with pytest.raises(ModelBehaviorError, match="non-empty string call ID"):
        await Runner.run(agent, "run it")

    assert callbacks == []
    assert executed == []


@pytest.mark.asyncio
async def test_empty_unresolved_function_call_id_fails_before_error_formatter() -> None:
    formatter_calls: list[str] = []

    def format_tool_error(args: Any) -> str:
        formatter_calls.append(args.tool_name)
        return "error"

    model = FakeModel(
        initial_output=[
            ResponseFunctionToolCall(
                id="item_0",
                type="function_call",
                name="missing",
                arguments="{}",
                call_id="",
            )
        ]
    )
    agent = Agent(name="agent", model=model)

    with pytest.raises(ModelBehaviorError, match="non-empty string call ID"):
        await Runner.run(
            agent,
            "run it",
            run_config=RunConfig(
                tool_error_formatter=format_tool_error,
                tool_not_found_behavior="return_error_to_model",
            ),
        )

    assert formatter_calls == []


@pytest.mark.asyncio
async def test_bound_call_id_with_missing_arguments_fails_before_error_formatter() -> None:
    executed: list[str] = []
    formatter_calls: list[str] = []

    @function_tool
    def record_value(context: ToolContext[Any], value: str) -> str:
        executed.append(value)
        context._restored_unbound_approval_call_ids.add("shared")
        return value

    def format_tool_error(args: Any) -> str:
        formatter_calls.append(args.tool_name)
        return "error"

    valid_call = get_function_tool_call(
        "record_value",
        json.dumps({"value": "safe"}),
        call_id="shared",
    )
    malformed_replacement = ResponseFunctionToolCall.model_construct(
        type="function_call",
        name="missing",
        call_id="shared",
    )
    model = FakeModel()
    model.add_multiple_turn_outputs([[valid_call], [malformed_replacement]])
    agent = Agent(name="agent", model=model, tools=[record_value])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(
            agent,
            "run it",
            run_config=RunConfig(
                tool_error_formatter=format_tool_error,
                tool_not_found_behavior="return_error_to_model",
            ),
        )

    assert executed == ["safe"]
    assert formatter_calls == []


@pytest.mark.asyncio
async def test_empty_handoff_call_id_fails_before_handoff_callback() -> None:
    handoff_calls: list[str] = []
    target = Agent(name="target")
    route = handoff(
        target,
        tool_name_override="route",
        on_handoff=lambda _context: handoff_calls.append("route"),
    )
    model = FakeModel(
        initial_output=[
            ResponseFunctionToolCall(
                id="item_0",
                type="function_call",
                name="route",
                arguments="{}",
                call_id="",
            )
        ]
    )
    agent = Agent(name="agent", model=model, handoffs=[route])

    with pytest.raises(ModelBehaviorError, match="non-empty string call ID"):
        await Runner.run(agent, "route it")

    assert handoff_calls == []


@pytest.mark.asyncio
async def test_failed_handoff_hook_does_not_commit_output_or_repeat_callback() -> None:
    hook_calls: list[str] = []
    target = Agent(name="target")
    call = get_handoff_tool_call(target, call_id="handoff_0")

    class FailingHooks(RunHooks[Any]):
        async def on_handoff(
            self,
            context: RunContextWrapper[Any],
            from_agent: Agent[Any],
            to_agent: Agent[Any],
        ) -> None:
            hook_calls.append(to_agent.name)
            raise RuntimeError("handoff hook failed")

    model = FakeModel(initial_output=[call])
    model.set_next_output([call.model_copy(deep=True)])
    agent = Agent(name="source", model=model, handoffs=[target])
    context = RunContextWrapper(context=None)
    hooks = FailingHooks()

    with pytest.raises(RuntimeError, match="handoff hook failed"):
        await Runner.run(agent, "handoff", context=context, hooks=hooks)

    assert context._tool_invocation_status(call, invocation_role="handoff") == (  # noqa: SLF001
        ("function_call", "handoff_0"),
        False,
        True,
    )
    with pytest.raises(ModelBehaviorError, match="already executed"):
        await Runner.run(agent, "handoff", context=context, hooks=hooks)

    assert hook_calls == ["target"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_changed_arguments_under_reused_call_id_fail_before_second_side_effect(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    agent, run_config, executed = _build_scenario("call_0", "changed")

    first = await _run(agent, "record a value", run_config=run_config, mode=mode)
    assert len(first.interruptions) == 1
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await _run(agent, state, run_config=run_config, mode=mode)

    assert executed == ["safe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_exact_replay_reuses_committed_output_without_executing_again(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    agent, run_config, executed = _build_scenario("call_0", "safe")

    first = await _run(agent, "record a value", run_config=run_config, mode=mode)
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await _run(agent, state, run_config=run_config, mode=mode)

    assert resumed.final_output == "done"
    assert resumed.interruptions == []
    assert executed == ["safe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_non_approval_identical_siblings_execute_once(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    executed: list[str] = []
    events: list[Any] = []

    @function_tool
    async def record_value(value: str) -> str:
        executed.append(value)
        return value

    duplicate = get_function_tool_call(
        "record_value",
        '{"value":"safe"}',
        call_id="call_0",
    )
    model = FakeModel(initial_output=[duplicate, duplicate.model_copy(deep=True)])
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[record_value])

    result = await _run(
        agent,
        "record a value",
        run_config=RunConfig(),
        mode=mode,
        events=events,
    )

    assert result.final_output == "done"
    assert executed == ["safe"]
    if mode == "streamed":
        assert [
            event.name for event in events if getattr(event, "name", None) == "tool_called"
        ] == ["tool_called"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_non_approval_completed_replay_does_not_execute_again(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    executed: list[str] = []

    @function_tool
    async def record_value(value: str) -> str:
        executed.append(value)
        return value

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("record_value", '{"value":"safe"}', call_id="call_0")],
            [get_function_tool_call("record_value", '{ "value" : "safe" }', call_id="call_0")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="agent", model=model, tools=[record_value])

    result = await _run(agent, "record a value", run_config=RunConfig(), mode=mode)

    assert result.final_output == "done"
    assert executed == ["safe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_non_approval_changed_completed_call_id_fails_before_execution(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    executed: list[str] = []
    events: list[Any] = []

    class RecordingHooks(RunHooks[Any]):
        def __init__(self) -> None:
            self.llm_end_calls = 0

        async def on_llm_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            response: ModelResponse,
        ) -> None:
            self.llm_end_calls += 1

    hooks = RecordingHooks()

    @function_tool
    async def record_value(value: str) -> str:
        executed.append(value)
        return value

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("record_value", '{"value":"safe"}', call_id="call_0")],
            [get_function_tool_call("record_value", '{"value":"changed"}', call_id="call_0")],
        ]
    )
    agent = Agent(name="agent", model=model, tools=[record_value])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await _run(
            agent,
            "record a value",
            run_config=RunConfig(),
            mode=mode,
            hooks=hooks,
            events=events,
        )

    assert executed == ["safe"]
    assert hooks.llm_end_calls == 1
    if mode == "streamed":
        assert [
            event.name for event in events if getattr(event, "name", None) == "tool_called"
        ] == ["tool_called"]


@pytest.mark.asyncio
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "serialized"])
async def test_nested_agent_tool_approval_allows_outer_call_id_collision(
    round_trip: bool,
) -> None:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    async def inner_sensitive_tool(value: str) -> str:
        executed.append(value)
        return value

    inner_model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "inner_sensitive_tool",
                '{"value":"safe"}',
                call_id="shared",
            )
        ]
    )
    inner_model.set_next_output([get_text_message("inner done")])
    inner_agent = Agent(name="inner", model=inner_model, tools=[inner_sensitive_tool])

    outer_model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "nested_agent",
                '{"input":"hello"}',
                call_id="shared",
            )
        ]
    )
    outer_model.set_next_output([get_text_message("outer done")])
    outer_agent = Agent(
        name="outer",
        model=outer_model,
        tools=[
            inner_agent.as_tool(
                tool_name="nested_agent",
                tool_description="Run the nested agent.",
            )
        ],
    )

    interrupted = await Runner.run(outer_agent, "start")
    assert len(interrupted.interruptions) == 1

    state = interrupted.to_state()
    if round_trip:
        state = await RunState.from_json(outer_agent, state.to_json())
    state.approve(state.get_interruptions()[0])
    resumed = await Runner.run(outer_agent, state)

    assert resumed.final_output == "outer done"
    assert resumed.interruptions == []
    assert executed == ["safe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "serialized"])
async def test_parent_approval_does_not_authorize_independent_nested_run(
    round_trip: bool,
) -> None:
    executed: list[str] = []

    @function_tool(name_override="sensitive", needs_approval=True)
    async def sensitive(value: str) -> str:
        executed.append(value)
        return value

    inner_model = FakeModel()
    inner_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("sensitive", '{"value":"same"}', call_id="shared")],
            [get_text_message("inner done")],
        ]
    )
    inner_agent = Agent(name="inner", model=inner_model, tools=[sensitive])

    outer_model = FakeModel()
    outer_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("sensitive", '{"value":"same"}', call_id="shared")],
            [
                get_function_tool_call(
                    "nested_agent",
                    '{"input":"hello"}',
                    call_id="outer-nested",
                )
            ],
            [get_text_message("outer done")],
        ]
    )
    outer_agent = Agent(
        name="outer",
        model=outer_model,
        tools=[
            sensitive,
            inner_agent.as_tool(
                tool_name="nested_agent",
                tool_description="Run the nested agent.",
            ),
        ],
    )

    parent_interruption = await Runner.run(outer_agent, "start")
    parent_state = parent_interruption.to_state()
    parent_state.approve(parent_state.get_interruptions()[0])
    nested_interruption = await Runner.run(outer_agent, parent_state)

    assert len(nested_interruption.interruptions) == 1
    assert executed == ["same"]

    nested_state = nested_interruption.to_state()
    if round_trip:
        nested_state = await RunState.from_json(outer_agent, nested_state.to_json())
    still_pending = await Runner.run(outer_agent, nested_state)

    assert len(still_pending.interruptions) == 1
    assert executed == ["same"]

    approved_state = still_pending.to_state()
    approved_state.approve(approved_state.get_interruptions()[0])
    completed = await Runner.run(outer_agent, approved_state)

    assert completed.final_output == "outer done"
    assert completed.interruptions == []
    assert executed == ["same", "same"]


@pytest.mark.asyncio
async def test_streamed_validated_items_precede_llm_end_hook_failure() -> None:
    executed: list[str] = []
    events: list[Any] = []

    class FailingHooks(RunHooks[Any]):
        async def on_llm_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            response: ModelResponse,
        ) -> None:
            _ = (context, agent, response)
            raise RuntimeError("llm end failed")

    @function_tool
    async def record_value(value: str) -> str:
        executed.append(value)
        return value

    model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "record_value",
                '{"value":"safe"}',
                call_id="call_0",
            )
        ]
    )
    agent = Agent(name="agent", model=model, tools=[record_value])
    result = Runner.run_streamed(agent, "record a value", hooks=FailingHooks())

    with pytest.raises(RuntimeError, match="llm end failed"):
        async for event in result.stream_events():
            events.append(event)

    assert [event.name for event in events if isinstance(event, RunItemStreamEvent)] == [
        "tool_called"
    ]
    assert executed == []


@pytest.mark.asyncio
async def test_non_approval_failed_tool_body_does_not_reexecute() -> None:
    attempts: list[str] = []

    @function_tool(failure_error_function=None)
    async def perform_side_effect() -> str:
        attempts.append("ran")
        raise RuntimeError("failed after side effect")

    call = get_function_tool_call("perform_side_effect", "{}", call_id="call_0")
    model = FakeModel()
    model.add_multiple_turn_outputs([[call], [call.model_copy(deep=True)]])
    agent = Agent(name="agent", model=model, tools=[perform_side_effect])
    context = RunContextWrapper(context=None)

    with pytest.raises(UserError, match="failed after side effect"):
        await Runner.run(agent, "run it", context=context)

    with pytest.raises(ModelBehaviorError, match="already executed"):
        await Runner.run(agent, "run it", context=context)

    assert attempts == ["ran"]


@pytest.mark.asyncio
async def test_non_approval_custom_tool_identical_siblings_execute_once() -> None:
    executed: list[str] = []

    async def invoke(_context: Any, value: str) -> str:
        executed.append(value)
        return value

    tool = CustomTool(
        name="raw_editor",
        description="Edit raw text.",
        on_invoke_tool=invoke,
        format={"type": "text"},
    )
    duplicate = ResponseCustomToolCall(
        type="custom_tool_call",
        name=tool.name,
        call_id="call_0",
        input="safe",
    )
    model = FakeModel(initial_output=[duplicate, duplicate.model_copy(deep=True)])
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[tool])

    result = await Runner.run(agent, "edit text")

    assert result.final_output == "done"
    assert executed == ["safe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_changed_same_id_siblings_fail_before_approval_callback(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    approval_calls: list[str] = []

    async def needs_approval(_context: Any, arguments: dict[str, Any], _call_id: str) -> bool:
        approval_calls.append(arguments["value"])
        return True

    @function_tool(needs_approval=needs_approval)
    async def record_value(value: str) -> str:
        return value

    model = FakeModel(
        initial_output=[
            get_function_tool_call("record_value", '{"value":"one"}', call_id="call_0"),
            get_function_tool_call("record_value", '{"value":"two"}', call_id="call_0"),
        ]
    )
    agent = Agent(name="agent", model=model, tools=[record_value])

    with pytest.raises(ModelBehaviorError, match="one response"):
        await _run(agent, "record values", run_config=RunConfig(), mode=mode)

    assert approval_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_response_processing_error_still_invokes_llm_end(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    class CountingHooks(RunHooks[Any]):
        def __init__(self) -> None:
            self.llm_end_calls = 0

        async def on_llm_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            response: ModelResponse,
        ) -> None:
            _ = (context, agent, response)
            self.llm_end_calls += 1

    hooks = CountingHooks()
    model = FakeModel(initial_output=[make_shell_call("call_0", commands=["echo safe"])])
    agent = Agent(name="agent", model=model)

    with pytest.raises(ModelBehaviorError, match="without a shell tool"):
        await _run(agent, "run command", run_config=RunConfig(), mode=mode, hooks=hooks)

    assert hooks.llm_end_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_processing_error_with_changed_call_id_still_suppresses_llm_end(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    class CountingHooks(RunHooks[Any]):
        def __init__(self) -> None:
            self.llm_end_calls = 0

        async def on_llm_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            response: ModelResponse,
        ) -> None:
            _ = (context, agent, response)
            self.llm_end_calls += 1

    @function_tool
    async def record_value(value: str) -> str:
        return value

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("record_value", '{"value":"safe"}', call_id="shared")],
            [
                get_function_tool_call(
                    "record_value",
                    '{"value":"changed"}',
                    call_id="shared",
                ),
                make_shell_call("shell_0", commands=["echo safe"]),
            ],
        ]
    )
    hooks = CountingHooks()
    agent = Agent(name="agent", model=model, tools=[record_value])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await _run(agent, "record value", run_config=RunConfig(), mode=mode, hooks=hooks)

    assert hooks.llm_end_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_processing_error_with_repeated_uncanonical_id_suppresses_llm_end(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    class CountingHooks(RunHooks[Any]):
        def __init__(self) -> None:
            self.llm_end_calls = 0

        async def on_llm_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            response: ModelResponse,
        ) -> None:
            _ = (context, agent, response)
            self.llm_end_calls += 1

    first_call = ResponseCustomToolCall.model_construct(
        type="custom_tool_call",
        name="first_missing_tool",
        call_id="shared",
    )
    second_call = ResponseCustomToolCall.model_construct(
        type="custom_tool_call",
        name="second_missing_tool",
        call_id="shared",
    )
    hooks = CountingHooks()
    model = FakeModel(initial_output=[first_call, second_call])
    agent = Agent(name="agent", model=model)

    with pytest.raises(ModelBehaviorError, match="one response"):
        await _run(agent, "run tool", run_config=RunConfig(), mode=mode, hooks=hooks)

    assert hooks.llm_end_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_changed_same_id_siblings_fail_before_non_approval_execution(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    executed: list[str] = []

    @function_tool
    async def record_value(value: str) -> str:
        executed.append(value)
        return value

    model = FakeModel(
        initial_output=[
            get_function_tool_call("record_value", '{"value":"one"}', call_id="call_0"),
            get_function_tool_call("record_value", '{"value":"two"}', call_id="call_0"),
        ]
    )
    agent = Agent(name="agent", model=model, tools=[record_value])

    with pytest.raises(ModelBehaviorError, match="one response"):
        await _run(agent, "record values", run_config=RunConfig(), mode=mode)

    assert executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_changed_computer_safety_checks_fail_before_same_response_effects(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    safety_checks: list[str] = []
    screenshots: list[str] = []

    class RecordingComputer(FakeComputer):
        def screenshot(self) -> str:
            screenshots.append("screenshot")
            return "img"

    def acknowledge_safety_check(data: Any) -> bool:
        safety_checks.append(data.safety_check.id)
        return True

    tool = ComputerTool(
        computer=RecordingComputer(),
        on_safety_check=acknowledge_safety_check,
    )
    first_call = ResponseComputerToolCall(
        id="computer-item-1",
        type="computer_call",
        action=ActionScreenshot(type="screenshot"),
        call_id="computer-call",
        pending_safety_checks=[PendingSafetyCheck(id="safety-1", code="code-1", message="first")],
        status="completed",
    )
    changed_call = first_call.model_copy(
        update={
            "id": "computer-item-2",
            "pending_safety_checks": [
                PendingSafetyCheck(id="safety-2", code="code-2", message="changed")
            ],
        }
    )
    agent = Agent(
        name="computer-agent",
        model=FakeModel(initial_output=[first_call, changed_call]),
        tools=[tool],
    )

    with pytest.raises(ModelBehaviorError, match="one response"):
        await _run(agent, "use computer", run_config=RunConfig(), mode=mode)

    assert safety_checks == []
    assert screenshots == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_changed_computer_safety_checks_fail_before_completed_replay_effects(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    safety_checks: list[str] = []
    screenshots: list[str] = []

    class RecordingComputer(FakeComputer):
        def screenshot(self) -> str:
            screenshots.append("screenshot")
            return "img"

    def acknowledge_safety_check(data: Any) -> bool:
        safety_checks.append(data.safety_check.id)
        return True

    tool = ComputerTool(
        computer=RecordingComputer(),
        on_safety_check=acknowledge_safety_check,
    )
    first_call = ResponseComputerToolCall(
        id="computer-item-1",
        type="computer_call",
        action=ActionScreenshot(type="screenshot"),
        call_id="computer-call",
        pending_safety_checks=[PendingSafetyCheck(id="safety-1", code="code-1", message="first")],
        status="completed",
    )
    changed_call = first_call.model_copy(
        update={
            "id": "computer-item-2",
            "pending_safety_checks": [
                PendingSafetyCheck(id="safety-2", code="code-2", message="changed")
            ],
        }
    )
    model = FakeModel()
    model.add_multiple_turn_outputs([[first_call], [changed_call]])
    agent = Agent(name="computer-agent", model=model, tools=[tool])

    with pytest.raises(ModelBehaviorError, match="completed tool call ID"):
        await _run(agent, "use computer", run_config=RunConfig(), mode=mode)

    assert safety_checks == ["safety-1"]
    assert screenshots == ["screenshot"]


@pytest.mark.asyncio
async def test_computer_hook_failure_does_not_repeat_side_effect() -> None:
    screenshots: list[str] = []

    class RecordingComputer(FakeComputer):
        def screenshot(self) -> str:
            screenshots.append("screenshot")
            return "img"

    class FailOnceHooks(RunHooks[Any]):
        def __init__(self) -> None:
            self.failed = False

        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Tool,
            result: object,
        ) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("end hook failed")

    tool = ComputerTool(computer=RecordingComputer())
    call = ResponseComputerToolCall(
        id="computer-item",
        type="computer_call",
        action=ActionScreenshot(type="screenshot"),
        call_id="computer-call",
        pending_safety_checks=[],
        status="completed",
    )
    model = FakeModel()
    model.add_multiple_turn_outputs([[call], [call.model_copy(deep=True)]])
    agent = Agent(name="computer-agent", model=model, tools=[tool])
    context = RunContextWrapper(context=None)
    hooks = FailOnceHooks()

    with pytest.raises(RuntimeError, match="end hook failed"):
        await Runner.run(agent, "use computer", context=context, hooks=hooks)

    with pytest.raises(ModelBehaviorError, match="already executed"):
        await Runner.run(agent, "use computer", context=context, hooks=hooks)

    assert screenshots == ["screenshot"]


@pytest.mark.asyncio
async def test_exact_replay_drops_tied_reasoning_item() -> None:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    async def record_value(value: str) -> str:
        executed.append(value)
        return value

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("record_value", '{"value":"safe"}', call_id="call_0")],
            [
                ResponseReasoningItem(id="rs_replay", summary=[], type="reasoning"),
                get_function_tool_call(
                    "record_value",
                    '{ "value" : "safe" }',
                    call_id="call_0",
                ),
            ],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="agent", model=model, tools=[record_value])
    first = await Runner.run(agent, "record a value")
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await Runner.run(agent, state)

    assert resumed.final_output == "done"
    assert executed == ["safe"]
    model_input = model.last_turn_args["input"]
    assert isinstance(model_input, list)
    assert not any(item.get("id") == "rs_replay" for item in model_input)
    assert (
        sum(
            item.get("type") == "function_call" and item.get("call_id") == "call_0"
            for item in model_input
        )
        == 1
    )


@pytest.mark.asyncio
async def test_streamed_exact_replay_does_not_emit_tied_reasoning_item() -> None:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    async def record_value(value: str) -> str:
        executed.append(value)
        return value

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("record_value", '{"value":"safe"}', call_id="call_0")],
            [
                ResponseReasoningItem(id="rs_replay", summary=[], type="reasoning"),
                get_function_tool_call(
                    "record_value",
                    '{ "value" : "safe" }',
                    call_id="call_0",
                ),
            ],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="agent", model=model, tools=[record_value])
    first = await Runner.run(agent, "record a value")
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = Runner.run_streamed(agent, state)
    item_events = [
        event async for event in resumed.stream_events() if isinstance(event, RunItemStreamEvent)
    ]

    assert resumed.final_output == "done"
    assert executed == ["safe"]
    assert not any(
        event.name == "reasoning_item_created"
        and getattr(event.item.raw_item, "id", None) == "rs_replay"
        for event in item_events
    )
    assert not any(event.name == "tool_called" for event in item_events)


@pytest.mark.asyncio
async def test_failed_tool_end_hook_does_not_reexecute_approved_call() -> None:
    agent, run_config, executed = _build_scenario("call_0", "safe")

    class FailOnceHooks(RunHooks[Any]):
        def __init__(self) -> None:
            self.failed = False

        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Tool,
            result: object,
        ) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("end hook failed")

    hooks = FailOnceHooks()
    first = await Runner.run(agent, "record a value", run_config=run_config)
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(UserError, match="end hook failed"):
        await Runner.run(agent, state, run_config=run_config, hooks=hooks)

    resumed = await Runner.run(agent, state, run_config=run_config, hooks=hooks)

    assert resumed.final_output == "done"
    assert executed == ["safe"]


@pytest.mark.asyncio
async def test_failed_output_guardrail_does_not_reexecute_approved_call() -> None:
    executed: list[str] = []

    @tool_output_guardrail
    async def reject_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.raise_exception(output_info="blocked")

    @function_tool(needs_approval=True, tool_output_guardrails=[reject_output])
    async def record_value() -> str:
        executed.append("ran")
        return "sensitive"

    model = FakeModel(
        initial_output=[get_function_tool_call("record_value", "{}", call_id="call_0")]
    )
    agent = Agent(name="agent", model=model, tools=[record_value])
    first = await Runner.run(agent, "record a value")
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(ToolOutputGuardrailTripwireTriggered):
        await Runner.run(agent, state)

    restored = await RunState.from_json(agent, state.to_json())
    with pytest.raises(ModelBehaviorError, match="already executed"):
        await Runner.run(agent, restored)

    assert executed == ["ran"]


@pytest.mark.asyncio
async def test_failed_approved_tool_body_does_not_reexecute() -> None:
    attempts: list[str] = []

    @function_tool(needs_approval=True, failure_error_function=None)
    async def perform_side_effect() -> str:
        attempts.append("ran")
        raise RuntimeError("failed after side effect")

    model = FakeModel(
        initial_output=[get_function_tool_call("perform_side_effect", "{}", call_id="call_0")]
    )
    agent = Agent(name="agent", model=model, tools=[perform_side_effect])
    first = await Runner.run(agent, "run it")
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(UserError, match="failed after side effect"):
        await Runner.run(agent, state)

    with pytest.raises(ModelBehaviorError, match="already executed"):
        await Runner.run(agent, state)

    assert attempts == ["ran"]


@pytest.mark.asyncio
async def test_cancelled_approved_tool_body_does_not_reexecute() -> None:
    attempts: list[str] = []
    started = asyncio.Event()
    keep_running = asyncio.Event()

    @function_tool(needs_approval=True, failure_error_function=None)
    async def perform_side_effect() -> str:
        attempts.append("ran")
        started.set()
        await keep_running.wait()
        return "done"

    model = FakeModel(
        initial_output=[get_function_tool_call("perform_side_effect", "{}", call_id="call_0")]
    )
    agent = Agent(name="agent", model=model, tools=[perform_side_effect])
    first = await Runner.run(agent, "run it")
    state = first.to_state()
    state.approve(first.interruptions[0])
    resume_task = asyncio.create_task(Runner.run(agent, state))
    await started.wait()
    resume_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await resume_task

    with pytest.raises(ModelBehaviorError, match="already executed"):
        await Runner.run(agent, state)

    assert attempts == ["ran"]


@pytest.mark.asyncio
async def test_failed_approved_agent_tool_start_does_not_reexecute() -> None:
    hook_calls: list[str] = []
    inner_agent = Agent(
        name="inner",
        model=FakeModel(initial_output=[get_text_message("inner done")]),
    )
    agent_tool = inner_agent.as_tool(
        tool_name="delegate",
        tool_description="Delegate work.",
        needs_approval=True,
    )

    class FailingHooks(RunHooks[Any]):
        async def on_tool_start(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Tool,
        ) -> None:
            if tool is agent_tool:
                hook_calls.append("ran")
                raise RuntimeError("failed after side effect")

    outer_model = FakeModel(
        initial_output=[get_function_tool_call("delegate", '{"input":"hi"}', call_id="call_0")]
    )
    outer_agent = Agent(name="outer", model=outer_model, tools=[agent_tool])
    first = await Runner.run(outer_agent, "delegate")
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(UserError, match="failed after side effect"):
        await Runner.run(outer_agent, state, hooks=FailingHooks())

    with pytest.raises(ModelBehaviorError, match="already executed"):
        await Runner.run(outer_agent, state, hooks=FailingHooks())

    assert hook_calls == ["ran"]


@pytest.mark.asyncio
async def test_failed_parallel_tool_end_hook_checkpoints_outputs_in_model_order() -> None:
    executed: list[str] = []
    first_finished = asyncio.Event()

    @function_tool(needs_approval=True)
    async def first_tool() -> str:
        await asyncio.sleep(0.01)
        executed.append("first")
        first_finished.set()
        return "first"

    @function_tool(needs_approval=True)
    async def second_tool() -> str:
        executed.append("second")
        return "second"

    class FailSecondHookOnce(RunHooks[Any]):
        def __init__(self) -> None:
            self.failed = False

        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool: Tool,
            result: object,
        ) -> None:
            if tool.name == "second_tool" and not self.failed:
                await first_finished.wait()
                self.failed = True
                raise RuntimeError("second end hook failed")

    model = FakeModel(
        initial_output=[
            get_function_tool_call("first_tool", "{}", call_id="call_first"),
            get_function_tool_call("second_tool", "{}", call_id="call_second"),
        ]
    )
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[first_tool, second_tool])
    hooks = FailSecondHookOnce()

    first = await Runner.run(agent, "run tools")
    state = first.to_state()
    for interruption in first.interruptions:
        state.approve(interruption)

    with pytest.raises(UserError, match="second end hook failed"):
        await Runner.run(agent, state, hooks=hooks)

    resumed = await Runner.run(agent, state, hooks=hooks)

    assert resumed.final_output == "done"
    assert sorted(executed) == ["first", "second"]
    model_input = model.last_turn_args["input"]
    assert isinstance(model_input, list)
    output_call_ids = [
        item["call_id"]
        for item in model_input
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    assert output_call_ids == ["call_first", "call_second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_identical_approval_bound_siblings_execute_once(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    def record_value(value: str) -> str:
        executed.append(value)
        return value

    duplicated_call = get_function_tool_call(
        "record_value",
        json.dumps({"value": "safe"}),
        call_id="call-duplicate",
    )
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [[duplicated_call, duplicated_call.model_copy(deep=True)], [get_text_message("done")]]
    )
    run_config = RunConfig(model_provider=_ScriptedProvider(model))
    agent = Agent(
        name="custom-provider-agent",
        model="scripted-provider-model",
        tools=[record_value],
    )

    first = await _run(agent, "record a value", run_config=run_config, mode=mode)
    assert len(first.interruptions) == 1
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await _run(agent, state, run_config=run_config, mode=mode)

    assert resumed.final_output == "done"
    assert executed == ["safe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_new_call_id_requires_a_new_per_call_approval(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    agent, run_config, executed = _build_scenario("call_1", "changed")

    first = await _run(agent, "record a value", run_config=run_config, mode=mode)
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await _run(agent, state, run_config=run_config, mode=mode)

    assert len(resumed.interruptions) == 1
    assert resumed.interruptions[0].arguments == json.dumps({"value": "changed"})
    assert executed == ["safe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False], ids=["always-approve", "always-reject"])
async def test_serialized_sticky_decision_rejects_changed_reused_call_id(
    approve: bool,
) -> None:
    agent, run_config, executed = _build_scenario("call_0", "changed")

    first = await Runner.run(agent, "record a value", run_config=run_config)
    state = first.to_state()
    if approve:
        state.approve(first.interruptions[0], always_approve=True)
    else:
        state.reject(first.interruptions[0], always_reject=True)
    restored_state = await RunState.from_json(agent, state.to_json())

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, restored_state, run_config=run_config)

    assert executed == (["safe"] if approve else [])


@pytest.mark.asyncio
async def test_sticky_upgrade_rejects_changed_reuse_of_prior_call_id() -> None:
    agent, run_config, executed = _build_scenario("call_0", "changed")

    first = await Runner.run(agent, "record a value", run_config=run_config)
    state = first.to_state()
    state.approve(first.interruptions[0])
    state.approve(first.interruptions[0], always_approve=True)

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, state, run_config=run_config)

    assert executed == ["safe"]


@pytest.mark.asyncio
async def test_serialized_sticky_identical_siblings_execute_once() -> None:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    async def record_value(value: int) -> str:
        executed.append(str(value))
        return str(value)

    duplicate = get_function_tool_call(
        "record_value",
        '{"value":1}',
        call_id="call_0",
    )
    model = FakeModel(
        initial_output=[
            duplicate,
            duplicate.model_copy(deep=True),
        ]
    )
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[record_value])

    first = await Runner.run(agent, "record a value")
    state = first.to_state()
    state.approve(first.interruptions[0], always_approve=True)
    restored = await RunState.from_json(agent, state.to_json())

    resumed = await Runner.run(agent, restored)

    assert resumed.final_output == "done"
    assert executed == ["1"]


@pytest.mark.parametrize("approve", [True, False], ids=["always-approve", "always-reject"])
def test_sticky_decision_preserves_prior_per_call_binding(approve: bool) -> None:
    agent = Agent(name="agent")
    context = RunContextWrapper(context=None)
    first = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_a", '{"value":1}', call_id="call_0"),
        ),
        tool_name="tool_a",
    )
    sticky = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_a", '{"value":2}', call_id="call_1"),
        ),
        tool_name="tool_a",
    )
    changed = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_a", '{"value":3}', call_id="call_0"),
        ),
        tool_name="tool_a",
    )
    context.approve_tool(first)
    if approve:
        context.approve_tool(sticky, always_approve=True)
    else:
        context.reject_tool(sticky, always_reject=True)

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        context.get_approval_status(
            "tool_a",
            "call_0",
            current_invocation=changed,
        )


@pytest.mark.parametrize("approve", [True, False], ids=["always-approve", "always-reject"])
def test_sticky_decision_does_not_disable_other_tool_binding(approve: bool) -> None:
    agent = Agent(name="agent")
    context = RunContextWrapper(context=None)
    other = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_b", '{"value":1}', call_id="call_0"),
        ),
        tool_name="tool_b",
    )
    sticky = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_a", '{"value":2}', call_id="call_1"),
        ),
        tool_name="tool_a",
    )
    changed_other = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_b", '{"value":3}', call_id="call_0"),
        ),
        tool_name="tool_b",
    )
    context.approve_tool(other)
    if approve:
        context.approve_tool(sticky, always_approve=True)
    else:
        context.reject_tool(sticky, always_reject=True)

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        context.get_approval_status(
            "tool_b",
            "call_0",
            current_invocation=changed_other,
        )


@pytest.mark.parametrize("approve", [True, False], ids=["always-approve", "always-reject"])
def test_matching_sticky_decision_does_not_mask_other_tool_binding(approve: bool) -> None:
    agent = Agent(name="agent")
    context = RunContextWrapper(context=None)
    other = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_b", '{"value":1}', call_id="call_0"),
        ),
        tool_name="tool_b",
    )
    sticky = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_a", '{"value":2}', call_id="call_1"),
        ),
        tool_name="tool_a",
    )
    current = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("tool_a", '{"value":3}', call_id="call_0"),
        ),
        tool_name="tool_a",
    )
    context.approve_tool(other)
    if approve:
        context.approve_tool(sticky, always_approve=True)
    else:
        context.reject_tool(sticky, always_reject=True)

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        context.get_approval_status(
            "tool_a",
            "call_0",
            current_invocation=current,
        )


@pytest.mark.parametrize("approve", [True, False], ids=["always-approve", "always-reject"])
def test_deferred_sticky_decision_preserves_mirrored_per_call_binding(approve: bool) -> None:
    agent = Agent(name="agent")
    context = RunContextWrapper(context=None)

    def approval_item(call_id: str, value: int) -> ToolApprovalItem:
        raw_item = cast(
            ResponseFunctionToolCall,
            get_function_tool_call(
                "lookup",
                json.dumps({"value": value}),
                call_id=call_id,
            ),
        )
        return ToolApprovalItem(
            agent=agent,
            raw_item=raw_item,
            tool_name="lookup",
            tool_namespace="lookup",
            tool_lookup_key=("deferred_top_level", "lookup"),
            _allow_bare_name_alias=True,
        )

    first = approval_item("call_0", 1)
    sticky = approval_item("call_1", 2)
    changed = approval_item("call_0", 3)
    context.approve_tool(first)
    if approve:
        context.approve_tool(sticky, always_approve=True)
    else:
        context.reject_tool(sticky, always_reject=True)

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        context.get_approval_status(
            "lookup",
            "call_0",
            tool_namespace="lookup",
            tool_lookup_key=("deferred_top_level", "lookup"),
            current_invocation=changed,
        )


def test_sticky_function_approval_rejects_same_id_shell_call() -> None:
    agent = Agent(name="agent")
    context = RunContextWrapper(context=None)
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("shell", "{}", call_id="call_0"),
        ),
        tool_name="shell",
    )
    context.approve_tool(approval_item, always_approve=True)
    shell_item = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "shell_call",
            "call_id": "call_0",
            "action": {"commands": ["echo safe"]},
        },
        tool_name="shell",
    )

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        context.get_approval_status(
            "shell",
            "call_0",
            current_invocation=shell_item,
        )


@pytest.mark.asyncio
async def test_changed_tool_under_approved_call_id_fails_before_second_tool_starts() -> None:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    def first_tool(value: str) -> str:
        executed.append(f"first:{value}")
        return value

    @function_tool(needs_approval=True)
    def second_tool(value: str) -> str:
        executed.append(f"second:{value}")
        return value

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "first_tool",
                    json.dumps({"value": "same"}),
                    call_id="call_0",
                )
            ],
            [
                get_function_tool_call(
                    "second_tool",
                    json.dumps({"value": "same"}),
                    call_id="call_0",
                )
            ],
        ]
    )
    run_config = RunConfig(model_provider=_ScriptedProvider(model))
    agent = Agent(
        name="custom-provider-agent",
        model="scripted-provider-model",
        tools=[first_tool, second_tool],
    )

    first = await Runner.run(agent, "run tools", run_config=run_config)
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, state, run_config=run_config)

    assert executed == ["first:same"]


@pytest.mark.asyncio
async def test_approved_call_id_reused_for_another_invocation_type_fails_before_execution() -> None:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    def approved_tool(value: str) -> str:
        executed.append(f"function:{value}")
        return value

    async def invoke_custom(_ctx: Any, raw_input: str) -> str:
        executed.append(f"custom:{raw_input}")
        return raw_input

    custom_tool = CustomTool(
        name="raw_editor",
        description="Edit raw text.",
        on_invoke_tool=invoke_custom,
        format={"type": "text"},
    )
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "approved_tool",
                    json.dumps({"value": "safe"}),
                    call_id="call_0",
                )
            ],
            [
                ResponseCustomToolCall(
                    type="custom_tool_call",
                    name="raw_editor",
                    call_id="call_0",
                    input="changed-kind",
                )
            ],
        ]
    )
    run_config = RunConfig(model_provider=_ScriptedProvider(model))
    agent = Agent(
        name="custom-provider-agent",
        model="scripted-provider-model",
        tools=[approved_tool, custom_tool],
    )

    first = await Runner.run(agent, "run tools", run_config=run_config)
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, state, run_config=run_config)

    assert executed == ["function:safe"]


@pytest.mark.asyncio
async def test_changed_missing_tool_under_approved_call_id_fails_before_sibling_tool() -> None:
    executed: list[str] = []
    approval_checks: list[str] = []

    @function_tool(needs_approval=True)
    def approved_tool(value: str) -> str:
        executed.append(f"approved:{value}")
        return value

    async def sibling_needs_approval(_ctx: Any, _args: dict[str, Any], call_id: str) -> bool:
        approval_checks.append(call_id)
        return False

    @function_tool(needs_approval=sibling_needs_approval)
    def sibling_tool() -> str:
        executed.append("sibling")
        return "sibling"

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "approved_tool",
                    json.dumps({"value": "safe"}),
                    call_id="call_0",
                )
            ],
            [
                get_function_tool_call("sibling_tool", "{}", call_id="call_1"),
                get_function_tool_call("missing_tool", "{}", call_id="call_0"),
            ],
        ]
    )
    run_config = RunConfig(
        model_provider=_ScriptedProvider(model),
        tool_not_found_behavior="return_error_to_model",
    )
    agent = Agent(
        name="custom-provider-agent",
        model="scripted-provider-model",
        tools=[approved_tool, sibling_tool],
    )

    first = await Runner.run(agent, "run tools", run_config=run_config)
    state = first.to_state()
    state.approve(first.interruptions[0])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, state, run_config=run_config)

    assert executed == ["approved:safe"]
    assert approval_checks == []


def _build_serialized_replay_scenario(
    replay_value: str,
) -> tuple[Agent[Any], RunConfig, list[str]]:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    def record_value(value: str) -> str:
        executed.append(f"record:{value}")
        return value

    @function_tool(needs_approval=True)
    def approval_gate(value: str) -> str:
        executed.append(f"gate:{value}")
        return value

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call(
                    "record_value",
                    json.dumps({"value": "safe"}),
                    call_id="call_0",
                )
            ],
            [
                get_function_tool_call(
                    "approval_gate",
                    json.dumps({"value": "pause"}),
                    call_id="call_1",
                )
            ],
            [
                get_function_tool_call(
                    "record_value",
                    json.dumps({"value": replay_value}, separators=(",", ":")),
                    call_id="call_0",
                )
            ],
            [get_text_message("done")],
        ]
    )
    run_config = RunConfig(model_provider=_ScriptedProvider(model))
    agent = Agent(
        name="custom-provider-agent",
        model="scripted-provider-model",
        tools=[record_value, approval_gate],
    )
    return agent, run_config, executed


@pytest.mark.asyncio
async def test_serialized_pending_approval_keeps_its_invocation_binding() -> None:
    agent, run_config, executed = _build_scenario("call_0", "changed")

    first = await Runner.run(agent, "record a value", run_config=run_config)
    state = first.to_state()
    state.approve(first.interruptions[0])
    restored = await RunState.from_string(agent, state.to_string())

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, restored, run_config=run_config)

    assert executed == ["safe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_serialized_completed_approval_rejects_changed_replay(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    agent, run_config, executed = _build_serialized_replay_scenario("changed")

    first = await _run(agent, "record a value", run_config=run_config, mode=mode)
    state = first.to_state()
    state.approve(first.interruptions[0])
    second = await _run(agent, state, run_config=run_config, mode=mode)
    restored = await RunState.from_string(agent, second.to_state().to_string())
    restored.approve(restored.get_interruptions()[0])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await _run(agent, restored, run_config=run_config, mode=mode)

    assert executed == ["record:safe", "gate:pause"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
async def test_serialized_completed_approval_skips_exact_replay(
    mode: Literal["non_streamed", "streamed"],
) -> None:
    agent, run_config, executed = _build_serialized_replay_scenario("safe")

    first = await _run(agent, "record a value", run_config=run_config, mode=mode)
    state = first.to_state()
    state.approve(first.interruptions[0])
    second = await _run(agent, state, run_config=run_config, mode=mode)
    restored = await RunState.from_string(agent, second.to_state().to_string())
    restored.approve(restored.get_interruptions()[0])

    result = await _run(agent, restored, run_config=run_config, mode=mode)

    assert result.final_output == "done"
    assert executed == ["record:safe", "gate:pause"]
    provider = run_config.model_provider
    assert isinstance(provider, _ScriptedProvider)
    model = provider.model
    assert isinstance(model, FakeModel)
    model_input = model.last_turn_args["input"]
    assert isinstance(model_input, list)
    for call_id in ("call_0", "call_1"):
        calls = [
            item
            for item in model_input
            if item.get("type") == "function_call" and item.get("call_id") == call_id
        ]
        outputs = [
            item
            for item in model_input
            if item.get("type") == "function_call_output" and item.get("call_id") == call_id
        ]
        assert len(calls) == 1
        assert len(outputs) == 1


async def _restore_as_schema_1_13(
    agent: Agent[Any],
    state: RunState[Any, Agent[Any]],
) -> RunState[Any, Agent[Any]]:
    json_data = state.to_json()
    json_data["$schemaVersion"] = "1.13"
    json_data["context"].pop("tool_invocations", None)
    return await RunState.from_json(agent, json_data)


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["1.13", "1.14"])
@pytest.mark.parametrize("mode", ["non_streamed", "streamed"])
@pytest.mark.parametrize("replay_value", ["safe", "changed"])
async def test_legacy_schema_historical_sticky_call_id_is_not_reexecuted(
    schema_version: str,
    mode: Literal["non_streamed", "streamed"],
    replay_value: str,
) -> None:
    executed: list[str] = []

    @function_tool(needs_approval=True)
    async def record_value(value: str) -> str:
        executed.append(value)
        return value

    historical_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call(
            "record_value",
            '{"value":"safe"}',
            call_id="call_0",
        ),
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "record_value",
                json.dumps({"value": replay_value}),
                call_id="call_0",
            )
        ]
    )
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[record_value])
    context: RunContextWrapper[Any] = RunContextWrapper(context=None)
    context.approve_tool(
        ToolApprovalItem(agent=agent, raw_item=historical_call),
        always_approve=True,
    )
    state = RunState(
        context=context,
        original_input=[
            historical_call.model_dump(exclude_none=True),
            {
                "type": "function_call_output",
                "call_id": "call_0",
                "output": "safe",
            },
        ],
        starting_agent=agent,
    )
    serialized = state.to_json()
    serialized["$schemaVersion"] = schema_version
    serialized["context"].pop("tool_invocations", None)
    restored = await RunState.from_json(agent, serialized)

    if replay_value == "changed":
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await _run(agent, restored, run_config=RunConfig(), mode=mode)
    else:
        result = await _run(agent, restored, run_config=RunConfig(), mode=mode)
        assert result.final_output == "done"

    assert executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["1.13", "1.14"])
async def test_legacy_changed_pending_call_fails_before_approval_callback(
    schema_version: str,
) -> None:
    approval_calls: list[str] = []

    async def needs_approval(_context: Any, arguments: dict[str, Any], _call_id: str) -> bool:
        approval_calls.append(arguments["value"])
        return True

    @function_tool(needs_approval=needs_approval)
    async def record_value(value: str) -> str:
        return value

    historical_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call(
            "record_value",
            '{"value":"safe"}',
            call_id="call_0",
        ),
    )
    changed_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call(
            "record_value",
            '{"value":"changed"}',
            call_id="call_0",
        ),
    )
    agent = Agent(name="agent", tools=[record_value])
    context: RunContextWrapper[Any] = RunContextWrapper(context=None)
    context.approve_tool(ToolApprovalItem(agent=agent, raw_item=historical_call))
    pending_item = ToolApprovalItem(agent=agent, raw_item=changed_call)
    state = make_state_with_interruptions(
        agent,
        [pending_item],
        original_input=cast(
            Any,
            [
                historical_call.model_dump(exclude_none=True),
                {
                    "type": "function_call_output",
                    "call_id": "call_0",
                    "output": "safe",
                },
            ],
        ),
    )
    state._context = context
    serialized = state.to_json()
    serialized["$schemaVersion"] = schema_version
    serialized["context"].pop("tool_invocations", None)
    restored = await RunState.from_json(agent, serialized)
    restored_pending = restored.get_interruptions()[0]
    run = ToolRunFunction(tool_call=changed_call, function_tool=record_value)

    async def build_rejection(_run: ToolRunFunction, _call_id: str) -> ToolApprovalItem:
        return restored_pending

    async def check_approval(_run: ToolRunFunction) -> bool:
        return await needs_approval(None, {"value": "changed"}, "call_0")

    assert restored._context is not None
    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await _collect_runs_by_approval(
            [run],
            call_id_extractor=lambda item: item.tool_call.call_id,
            tool_name_resolver=lambda item: item.function_tool.name,
            rejection_builder=build_rejection,
            context_wrapper=restored._context,
            approval_items_by_call_id={"call_0": restored_pending},
            agent=agent,
            pending_interruption_adder=lambda _item: None,
            needs_approval_checker=check_approval,
        )

    assert approval_calls == []


@pytest.mark.asyncio
async def test_schema_1_13_completed_approval_rejects_changed_replay() -> None:
    agent, run_config, executed = _build_serialized_replay_scenario("changed")

    first = await Runner.run(agent, "record a value", run_config=run_config)
    state = first.to_state()
    state.approve(first.interruptions[0])
    second = await Runner.run(agent, state, run_config=run_config)
    restored = await _restore_as_schema_1_13(agent, second.to_state())
    restored.approve(restored.get_interruptions()[0])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, restored, run_config=run_config)

    assert executed == ["record:safe", "gate:pause"]


@pytest.mark.asyncio
async def test_schema_1_13_completed_approval_skips_exact_replay() -> None:
    agent, run_config, executed = _build_serialized_replay_scenario("safe")

    first = await Runner.run(agent, "record a value", run_config=run_config)
    state = first.to_state()
    state.approve(first.interruptions[0])
    second = await Runner.run(agent, state, run_config=run_config)
    restored = await _restore_as_schema_1_13(agent, second.to_state())
    restored.approve(restored.get_interruptions()[0])

    result = await Runner.run(agent, restored, run_config=run_config)

    assert result.final_output == "done"
    assert executed == ["record:safe", "gate:pause"]


def _build_mcp_approval_request(
    agent: Agent[Any],
    *,
    name: str = "lookup",
    server_label: str = "test_server",
    call_id: str = "mcp_call_0",
    arguments: str | None = None,
) -> tuple[Any, ToolApprovalItem]:
    request_item = McpApprovalRequest(
        id=call_id,
        type="mcp_approval_request",
        name=name,
        server_label=server_label,
        arguments=arguments or json.dumps({"query": "safe"}),
    )
    request = SimpleNamespace(
        request_item=request_item,
        mcp_tool=SimpleNamespace(name=name, on_approval_request=None),
    )
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item=request_item,
        tool_name=name,
    )
    return request, approval_item


def test_wrapped_mcp_approval_uses_the_same_canonical_request_id() -> None:
    agent = Agent(name="mcp-approval-agent")
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "hosted_tool_call",
            "id": "outer-item-id",
            "call_id": "shared-request",
            "name": "lookup",
            "provider_data": {
                "type": "mcp_approval_request",
                "name": "lookup",
                "server_label": "test_server",
                "arguments": '{"query":"safe"}',
            },
        },
        tool_name="lookup",
    )
    changed_request, _ = _build_mcp_approval_request(
        agent,
        call_id="shared-request",
        arguments='{"query":"changed"}',
    )
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    context.approve_tool(approval_item)

    assert set(context._tool_invocations) == {"shared-request"}
    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        collect_manual_mcp_approvals(
            agent=agent,
            requests=[changed_request],
            context_wrapper=context,
            existing_pending_by_call_id={"shared-request": approval_item},
        )


def test_sticky_mcp_approval_rejects_same_id_on_another_server() -> None:
    agent = Agent(name="mcp-approval-agent")
    _, approval_item = _build_mcp_approval_request(agent, server_label="server_a")
    changed_request, _ = _build_mcp_approval_request(agent, server_label="server_b")
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    context.approve_tool(approval_item, always_approve=True)

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        collect_manual_mcp_approvals(
            agent=agent,
            requests=[changed_request],
            context_wrapper=context,
            existing_pending_by_call_id={"mcp_call_0": approval_item},
        )


def test_sticky_mcp_approval_reprompts_new_id_on_another_server() -> None:
    agent = Agent(name="mcp-approval-agent")
    _, approval_item = _build_mcp_approval_request(agent, server_label="server_a")
    changed_request, _ = _build_mcp_approval_request(
        agent,
        server_label="server_b",
        call_id="mcp_call_1",
    )
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    context.approve_tool(approval_item, always_approve=True)

    responses, pending = collect_manual_mcp_approvals(
        agent=agent,
        requests=[changed_request],
        context_wrapper=context,
        existing_pending_by_call_id={},
    )

    assert responses == []
    assert len(pending) == 1
    assert pending[0].raw_item is changed_request.request_item


@pytest.mark.asyncio
async def test_serialized_sticky_mcp_scope_reprompts_another_server() -> None:
    agent = Agent(name="mcp-approval-agent")
    _, approval_item = _build_mcp_approval_request(agent, server_label="server_a")
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    state = RunState(context=context, original_input="", starting_agent=agent)
    state.approve(approval_item, always_approve=True)
    restored = await RunState.from_json(agent, state.to_json())
    assert restored._context is not None
    changed_request, _ = _build_mcp_approval_request(
        agent,
        server_label="server_b",
        call_id="mcp_call_1",
    )

    responses, pending = collect_manual_mcp_approvals(
        agent=agent,
        requests=[changed_request],
        context_wrapper=restored._context,
        existing_pending_by_call_id={},
    )

    assert responses == []
    assert len(pending) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["1.13", "1.14"])
@pytest.mark.parametrize("missing_field", ["arguments", "server_label"])
async def test_unbindable_legacy_mcp_approval_requires_current_reapproval(
    schema_version: str,
    missing_field: str,
) -> None:
    agent = Agent(name="mcp-approval-agent")
    current_request, _ = _build_mcp_approval_request(agent)
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item=current_request.request_item,
        tool_name="lookup",
    )
    state = make_state_with_interruptions(agent, [approval_item])
    assert state._context is not None
    state._context._rebuild_approvals(  # noqa: SLF001
        {
            "lookup": {
                "approved": ["mcp_call_0"],
                "rejected": [],
            }
        }
    )
    serialized = state.to_json()
    serialized["$schemaVersion"] = schema_version
    serialized["context"].pop("tool_invocations", None)
    serialized["current_step"]["data"]["interruptions"][0]["raw_item"].pop(missing_field)

    restored = await RunState.from_json(agent, serialized)
    assert restored._context is not None
    restored_item = restored.get_interruptions()[0]

    responses, pending = collect_manual_mcp_approvals(
        agent=agent,
        requests=[current_request],
        context_wrapper=restored._context,
        existing_pending_by_call_id={"mcp_call_0": restored_item},
    )

    assert responses == []
    assert len(pending) == 1
    assert pending[0].raw_item is current_request.request_item


def _build_unbindable_current_mcp_request() -> Any:
    request_item = McpApprovalRequest.model_construct(
        id="mcp_call_0",
        type="mcp_approval_request",
        name="lookup",
        server_label="test_server",
    )
    return SimpleNamespace(
        request_item=request_item,
        mcp_tool=SimpleNamespace(name="lookup", on_approval_request=None),
    )


@pytest.mark.asyncio
async def test_unbindable_mcp_callback_request_requires_manual_reapproval() -> None:
    callback_calls = 0

    def approve_request(_request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        nonlocal callback_calls
        callback_calls += 1
        return {"approve": True}

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=approve_request,
    )
    request = McpApprovalRequest.model_construct(
        id="mcp_call_0",
        type="mcp_approval_request",
        name="lookup",
        server_label="test_server",
    )
    agent = Agent(
        name="mcp-approval-agent",
        model=FakeModel(initial_output=[request]),
        tools=[mcp_tool],
    )

    result = await Runner.run(agent, "lookup")

    assert callback_calls == 0
    assert len(result.interruptions) == 1
    assert result.interruptions[0].raw_item is request


@pytest.mark.parametrize("always_approve", [False, True], ids=["per-call", "sticky"])
def test_unbindable_current_manual_mcp_request_requires_reapproval(
    always_approve: bool,
) -> None:
    agent = Agent(name="mcp-approval-agent")
    _, approval_item = _build_mcp_approval_request(agent)
    current_request = _build_unbindable_current_mcp_request()
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    context.approve_tool(approval_item, always_approve=always_approve)

    responses, pending = collect_manual_mcp_approvals(
        agent=agent,
        requests=[current_request],
        context_wrapper=context,
        existing_pending_by_call_id={"mcp_call_0": approval_item},
    )

    assert responses == []
    assert len(pending) == 1
    assert pending[0].raw_item is current_request.request_item


def test_unbindable_current_hosted_mcp_request_requires_reapproval() -> None:
    agent = Agent(name="mcp-approval-agent")
    _, approval_item = _build_mcp_approval_request(agent)
    current_request = _build_unbindable_current_mcp_request()
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    context.approve_tool(approval_item)
    appended: list[Any] = []

    pending, pending_ids = process_hosted_mcp_approvals(
        original_pre_step_items=[approval_item],
        mcp_approval_requests=[current_request],
        context_wrapper=context,
        agent=agent,
        append_item=appended.append,
    )

    assert len(pending) == 1
    assert pending[0].raw_item is current_request.request_item
    assert pending_ids == {"mcp_call_0"}
    assert appended == pending


@pytest.mark.parametrize("with_callback", [False, True], ids=["manual", "callback"])
@pytest.mark.asyncio
async def test_runner_omits_completed_mcp_approval_request_replay(
    with_callback: bool,
) -> None:
    callback_calls = 0

    def approve_request(_request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        nonlocal callback_calls
        callback_calls += 1
        return {"approve": True}

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=approve_request if with_callback else None,
    )
    first_request = McpApprovalRequest(
        id="mcp_call_0",
        type="mcp_approval_request",
        name="lookup",
        server_label="test_server",
        arguments='{"query": "safe", "limit": 1}',
    )
    replayed_request = McpApprovalRequest(
        id="mcp_call_0",
        type="mcp_approval_request",
        name="lookup",
        server_label="test_server",
        arguments='{"limit":1,"query":"safe"}',
    )
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [[first_request], [replayed_request], [get_text_message("done")]]
    )
    agent = Agent(name="mcp-approval-agent", model=model, tools=[mcp_tool])

    first = await Runner.run(agent, "lookup")
    if with_callback:
        result = first
    else:
        assert len(first.interruptions) == 1
        state = first.to_state()
        state.approve(first.interruptions[0])
        result = await Runner.run(agent, state)

    assert result.final_output == "done"
    assert callback_calls == int(with_callback)
    model_input = model.last_turn_args["input"]
    assert isinstance(model_input, list)
    replay_items = [
        item
        for item in model_input
        if item.get("type") == "mcp_approval_request" and item.get("id") == "mcp_call_0"
    ]
    approval_responses = [
        item
        for item in model_input
        if item.get("type") == "mcp_approval_response"
        and item.get("approval_request_id") == "mcp_call_0"
    ]
    assert len(replay_items) == 1
    assert len(approval_responses) == 1


@pytest.mark.asyncio
async def test_serialized_completed_manual_mcp_approval_skips_exact_replay() -> None:
    agent = Agent(name="mcp-approval-agent")
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    request, approval_item = _build_mcp_approval_request(agent)
    state = RunState(context=context, original_input="", starting_agent=agent)
    state.approve(approval_item)

    first_responses, first_pending = collect_manual_mcp_approvals(
        agent=agent,
        requests=[request],
        context_wrapper=context,
        existing_pending_by_call_id={"mcp_call_0": approval_item},
    )

    assert len(first_responses) == 1
    assert first_pending == []
    context._mark_tool_call_completed(first_responses[0].raw_item)
    state._generated_items = [approval_item, first_responses[0]]

    restored = await RunState.from_string(agent, state.to_string())
    assert restored._context is not None
    replayed_responses, replayed_pending = collect_manual_mcp_approvals(
        agent=agent,
        requests=[request],
        context_wrapper=restored._context,
        existing_pending_by_call_id={"mcp_call_0": approval_item},
    )

    assert replayed_responses == []
    assert replayed_pending == []


def test_completed_hosted_mcp_approval_reconciliation_skips_exact_replay() -> None:
    agent = Agent(name="mcp-approval-agent")
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    request, approval_item = _build_mcp_approval_request(agent)
    context.approve_tool(approval_item)
    appended: list[Any] = []

    process_hosted_mcp_approvals(
        original_pre_step_items=[approval_item],
        mcp_approval_requests=[request],
        context_wrapper=context,
        agent=agent,
        append_item=appended.append,
    )

    assert len(appended) == 1
    context._mark_tool_call_completed(appended[0].raw_item)
    appended.clear()

    pending, pending_ids = process_hosted_mcp_approvals(
        original_pre_step_items=[approval_item],
        mcp_approval_requests=[request],
        context_wrapper=context,
        agent=agent,
        append_item=appended.append,
    )

    assert appended == []
    assert pending == []
    assert pending_ids == set()


def test_sticky_manual_mcp_approval_rejects_same_id_for_changed_tool_name() -> None:
    agent = Agent(name="mcp-approval-agent")
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    _, approval_item = _build_mcp_approval_request(agent)
    context.approve_tool(approval_item, always_approve=True)

    same_tool, _ = _build_mcp_approval_request(
        agent,
        arguments=json.dumps({"query": "changed"}),
    )
    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        collect_manual_mcp_approvals(
            agent=agent,
            requests=[same_tool],
            context_wrapper=context,
            existing_pending_by_call_id={"mcp_call_0": approval_item},
        )
    changed_context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    changed_context.approve_tool(approval_item, always_approve=True)
    changed_tool, _ = _build_mcp_approval_request(
        agent,
        name="delete_all",
        arguments=json.dumps({"confirm": True}),
    )
    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        collect_manual_mcp_approvals(
            agent=agent,
            requests=[changed_tool],
            context_wrapper=changed_context,
            existing_pending_by_call_id={"mcp_call_0": approval_item},
        )


def test_sticky_hosted_mcp_approval_rejects_same_id_for_changed_tool_name() -> None:
    agent = Agent(name="mcp-approval-agent")
    context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    _, approval_item = _build_mcp_approval_request(agent)
    context.approve_tool(approval_item, always_approve=True)

    same_tool, _ = _build_mcp_approval_request(
        agent,
        arguments=json.dumps({"query": "changed"}),
    )
    same_appended: list[Any] = []
    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        process_hosted_mcp_approvals(
            original_pre_step_items=[approval_item],
            mcp_approval_requests=[same_tool],
            context_wrapper=context,
            agent=agent,
            append_item=same_appended.append,
        )
    changed_context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    changed_context.approve_tool(approval_item, always_approve=True)
    changed_tool, _ = _build_mcp_approval_request(
        agent,
        name="delete_all",
        arguments=json.dumps({"confirm": True}),
    )
    changed_appended: list[Any] = []
    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        process_hosted_mcp_approvals(
            original_pre_step_items=[approval_item],
            mcp_approval_requests=[changed_tool],
            context_wrapper=changed_context,
            agent=agent,
            append_item=changed_appended.append,
        )

    assert same_appended == []
    assert changed_appended == []
