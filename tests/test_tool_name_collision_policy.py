from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_function_tool_call import CallerProgram
from openai.types.responses.response_output_item import Program

from agents import (
    Agent,
    ApplyPatchTool,
    ModelBehaviorError,
    ProgrammaticToolCallingTool,
    RunConfig,
    RunContextWrapper,
    Runner,
    RunState,
    ShellTool,
    UserError,
    handoff,
    tool_namespace,
)
from agents.items import ToolCallOutputItem
from agents.lifecycle import RunHooks
from agents.tool import Tool, function_tool

from .fake_model import FakeModel
from .mcp.helpers import FakeMCPServer
from .test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message


def _record(calls: list[str], value: str, result: str | None = None) -> str:
    calls.append(value)
    return value if result is None else result


@pytest.mark.asyncio
async def test_resume_warn_mode_rebinds_queued_mcp_call_to_local_winner() -> None:
    calls: list[str] = []
    server = FakeMCPServer(require_approval="always")
    server.add_tool("lookup", {"type": "object", "properties": {}})

    def local_lookup() -> str:
        calls.append("local")
        return "local"

    local_tool = function_tool(local_lookup, name_override="lookup")
    model = FakeModel(initial_output=[get_function_tool_call("lookup", "{}")])
    agent = Agent(name="agent", model=model, mcp_servers=[server])

    initial_result = await Runner.run(agent, "Look this up")
    state = await RunState.from_json(agent, initial_result.to_state().to_json())
    interruption = state.get_interruptions()[0]
    state.approve(interruption)
    agent.tools = [local_tool]
    model.set_next_output([get_text_message("done")])

    resumed_result = await Runner.run(agent, state)

    assert resumed_result.final_output == "done"
    assert calls == ["local"]
    assert server.tool_calls == []


@pytest.mark.asyncio
async def test_resume_error_mode_rejects_current_collision_before_side_effects() -> None:
    calls: list[str] = []
    queued_tool = function_tool(
        lambda: _record(calls, "queued"),
        name_override="lookup",
        needs_approval=True,
    )
    colliding_tool = function_tool(
        lambda: _record(calls, "colliding"),
        name_override="lookup",
    )
    model = FakeModel(
        initial_output=[get_function_tool_call("lookup", "{}", call_id="lookup_call")]
    )
    agent = Agent(name="agent", model=model, tools=[queued_tool])

    initial_result = await Runner.run(agent, "Look this up")
    state = initial_result.to_state()
    state.approve(state.get_interruptions()[0])
    agent.tools = [queued_tool, colliding_tool]

    with pytest.raises(UserError, match="Ambiguous function tool configuration"):
        await Runner.run(
            agent,
            state,
            run_config=RunConfig(tool_name_collision_policy="error"),
        )

    assert calls == []


@pytest.mark.parametrize("deserialize", [False, True])
@pytest.mark.asyncio
async def test_resume_rejects_function_approval_reclassified_as_handoff(
    deserialize: bool,
) -> None:
    calls: list[str] = []
    filter_calls: list[str] = []

    class FalsyFilter:
        def __bool__(self) -> bool:
            return False

        def __call__(self, data: Any) -> Any:
            filter_calls.append("filter")
            return data

    def route_function() -> str:
        calls.append("function")
        return "function"

    route_tool = function_tool(
        route_function,
        name_override="route",
        needs_approval=True,
    )
    target = Agent(
        name="target",
        model=FakeModel(initial_output=[get_text_message("target done")]),
    )
    route_handoff = handoff(
        target,
        tool_name_override="route",
        on_handoff=lambda _: calls.append("handoff"),
        input_filter=FalsyFilter(),
    )
    model = FakeModel(initial_output=[get_function_tool_call("route", "{}", call_id="route_call")])
    agent = Agent(name="agent", model=model, tools=[route_tool])

    initial_result = await Runner.run(agent, "Route this request")
    state_json = initial_result.to_state().to_json()
    agent.tools = []
    agent.handoffs = [route_handoff]
    state = (
        await RunState.from_json(agent, state_json) if deserialize else initial_result.to_state()
    )
    state._model_responses[-1] = replace(state._model_responses[-1], output=[])
    state.approve(state.get_interruptions()[0])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, state)

    assert calls == []
    assert filter_calls == []


@pytest.mark.asyncio
async def test_reclassified_handoff_is_rejected_before_run_hook() -> None:
    calls: list[str] = []

    route_tool = function_tool(
        lambda: "function",
        name_override="route",
        needs_approval=True,
    )
    target = Agent(
        name="target",
        model=FakeModel(initial_output=[get_text_message("target done")]),
    )
    route_handoff = handoff(
        target,
        tool_name_override="route",
        on_handoff=lambda _: calls.append("handoff"),
    )
    model = FakeModel(initial_output=[get_function_tool_call("route", "{}", call_id="route")])
    agent = Agent(name="agent", model=model, tools=[route_tool])

    first = await Runner.run(agent, "Route this request")
    state = first.to_state()
    state.approve(state.get_interruptions()[0])
    state._model_responses[-1] = replace(state._model_responses[-1], output=[])
    agent.tools = []
    agent.handoffs = [route_handoff]

    hook_calls: list[str] = []

    class RecordingHandoffHooks(RunHooks[Any]):
        async def on_handoff(
            self,
            context: RunContextWrapper[Any],
            from_agent: Agent[Any],
            to_agent: Agent[Any],
        ) -> None:
            hook_calls.append("handoff")

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, state, hooks=RecordingHandoffHooks())

    assert calls == []
    assert hook_calls == []


@pytest.mark.asyncio
async def test_resume_rejects_queued_handoff_reclassified_as_function() -> None:
    calls: list[str] = []

    def approved_function() -> str:
        calls.append("approved")
        return "approved"

    def route_function() -> str:
        calls.append("route")
        return "route"

    approval_tool = function_tool(
        approved_function,
        name_override="approval_tool",
        needs_approval=True,
    )
    route_tool = function_tool(route_function, name_override="route")
    target = Agent(name="target")
    route_handoff = handoff(target, tool_name_override="route")
    model = FakeModel(
        initial_output=[
            get_function_tool_call("approval_tool", "{}", call_id="approval_call"),
            get_handoff_tool_call(target, override_name="route", args="{}"),
        ]
    )
    model.set_next_output([get_text_message("done")])
    agent = Agent(
        name="agent",
        model=model,
        tools=[approval_tool],
        handoffs=[route_handoff],
    )

    initial_result = await Runner.run(agent, "Route this request")
    state = initial_result.to_state()
    state.approve(state.get_interruptions()[0])
    agent.tools = [approval_tool, route_tool]
    agent.handoffs = []
    state._model_responses[-1] = replace(state._model_responses[-1], output=[])

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, state)

    assert calls == []


@pytest.mark.asyncio
async def test_resume_rebinds_queued_handoff_to_current_warn_winner() -> None:
    calls: list[str] = []

    approval_tool = function_tool(
        lambda: _record(calls, "approved"),
        name_override="approval_tool",
        needs_approval=True,
    )
    first_target = Agent(
        name="first",
        model=FakeModel(initial_output=[get_text_message("first done")]),
    )
    second_target = Agent(
        name="second",
        model=FakeModel(initial_output=[get_text_message("second done")]),
    )
    first_handoff = handoff(
        first_target,
        tool_name_override="route",
        on_handoff=lambda _: calls.append("first"),
    )
    second_handoff = handoff(
        second_target,
        tool_name_override="route",
        on_handoff=lambda _: calls.append("second"),
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("approval_tool", "{}", call_id="approval_call"),
            get_handoff_tool_call(second_target, override_name="route", args="{}"),
        ]
    )
    agent = Agent(
        name="agent",
        model=model,
        tools=[approval_tool],
        handoffs=[first_handoff, second_handoff],
    )

    initial_result = await Runner.run(agent, "Route this request")
    state = initial_result.to_state()
    state.approve(state.get_interruptions()[0])
    agent.handoffs = [second_handoff, first_handoff]

    resumed_result = await Runner.run(agent, state)

    assert resumed_result.final_output == "first done"
    assert calls == ["approved", "first"]


@pytest.mark.asyncio
async def test_resume_rejects_missing_queued_handoff_before_side_effects() -> None:
    calls: list[str] = []
    approval_tool = function_tool(
        lambda: _record(calls, "approved"),
        name_override="approval_tool",
        needs_approval=True,
    )
    target = Agent(name="target")
    route_handoff = handoff(
        target,
        tool_name_override="route",
        on_handoff=lambda _: calls.append("handoff"),
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("approval_tool", "{}", call_id="approval_call"),
            get_handoff_tool_call(target, override_name="route", args="{}"),
        ]
    )
    agent = Agent(
        name="agent",
        model=model,
        tools=[approval_tool],
        handoffs=[route_handoff],
    )

    initial_result = await Runner.run(agent, "Route this request")
    state = initial_result.to_state()
    state.approve(state.get_interruptions()[0])
    agent.handoffs = []

    with pytest.raises(ModelBehaviorError, match="Tool route not found in agent agent"):
        await Runner.run(agent, state)

    assert calls == []


@pytest.mark.asyncio
async def test_missing_interrupted_agent_tool_preserves_nested_state_for_retry() -> None:
    calls: list[str] = []
    before_tool = function_tool(
        lambda: _record(calls, "before"),
        name_override="before_pause",
    )
    sensitive_tool = function_tool(
        lambda: _record(calls, "sensitive"),
        name_override="sensitive",
        needs_approval=True,
    )
    inner_model = FakeModel(
        initial_output=[
            get_function_tool_call("before_pause", "{}", call_id="before_call"),
            get_function_tool_call("sensitive", "{}", call_id="sensitive_call"),
        ]
    )
    inner_model.set_next_output([get_text_message("inner done")])
    inner_agent = Agent(
        name="inner",
        model=inner_model,
        tools=[before_tool, sensitive_tool],
    )
    nested_tool = inner_agent.as_tool(
        tool_name="lookup",
        tool_description="Look up a value with the inner agent.",
    )
    outer_model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "lookup",
                '{"input":"hi"}',
                call_id="outer_call",
            )
        ]
    )
    outer_model.set_next_output([get_text_message("outer done")])
    outer_agent = Agent(name="outer", model=outer_model, tools=[nested_tool])

    initial_result = await Runner.run(outer_agent, "Look this up")
    state = await RunState.from_json(outer_agent, initial_result.to_state().to_json())
    state.approve(state.get_interruptions()[0])
    assert calls == ["before"]

    outer_agent.tools = []
    with pytest.raises(
        ModelBehaviorError,
        match="Tool lookup not found in agent outer",
    ) as strict_error:
        await Runner.run(outer_agent, state)

    outer_agent.tools = [nested_tool]
    resumed_result = await Runner.run(outer_agent, state)

    assert resumed_result.final_output == "outer done"
    assert calls == ["before", "sensitive"]
    assert strict_error.value is not None


@pytest.mark.asyncio
async def test_missing_formatter_cancellation_keeps_nested_state_serializable() -> None:
    calls: list[str] = []
    formatter_started = asyncio.Event()
    keep_formatter_waiting = asyncio.Event()
    before_tool = function_tool(
        lambda: _record(calls, "serial_before"),
        name_override="serial_before_pause",
    )
    sensitive_tool = function_tool(
        lambda: _record(calls, "serial_sensitive"),
        name_override="serial_sensitive",
        needs_approval=True,
    )
    inner_model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "serial_before_pause",
                "{}",
                call_id="serial_before_call",
            ),
            get_function_tool_call(
                "serial_sensitive",
                "{}",
                call_id="serial_sensitive_call",
            ),
        ]
    )
    inner_model.set_next_output([get_text_message("inner done")])
    inner_agent = Agent(
        name="inner",
        model=inner_model,
        tools=[before_tool, sensitive_tool],
    )
    nested_tool = inner_agent.as_tool(
        tool_name="serial_lookup",
        tool_description="Look up a value with the inner agent.",
    )
    outer_model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "serial_lookup",
                '{"input":"hi"}',
                call_id="serial_outer_call",
            )
        ]
    )
    outer_model.set_next_output([get_text_message("outer done")])
    outer_agent = Agent(name="outer", model=outer_model, tools=[nested_tool])

    initial_result = await Runner.run(outer_agent, "Look this up")
    state = await RunState.from_json(outer_agent, initial_result.to_state().to_json())
    state.approve(state.get_interruptions()[0])
    assert calls == ["serial_before"]
    outer_agent.tools = []

    async def blocking_formatter(_args: Any) -> str:
        formatter_started.set()
        await keep_formatter_waiting.wait()
        return "missing"

    resume_task = asyncio.create_task(
        Runner.run(
            outer_agent,
            state,
            run_config=RunConfig(
                tool_not_found_behavior="return_error_to_model",
                tool_error_formatter=blocking_formatter,
            ),
        )
    )
    await formatter_started.wait()
    resume_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resume_task

    outer_agent.tools = [nested_tool]
    restored_state = await RunState.from_json(outer_agent, state.to_json())
    resumed_result = await Runner.run(outer_agent, restored_state)

    assert resumed_result.final_output == "outer done"
    assert calls == ["serial_before", "serial_sensitive"]


@pytest.mark.asyncio
async def test_replacing_interrupted_agent_tool_fails_before_side_effects() -> None:
    calls: list[str] = []
    sensitive_tool = function_tool(
        lambda: "sensitive",
        name_override="sensitive",
        needs_approval=True,
    )
    inner_agent = Agent(
        name="inner",
        model=FakeModel(
            initial_output=[get_function_tool_call("sensitive", "{}", call_id="call_sensitive")]
        ),
        tools=[sensitive_tool],
    )
    nested_tool = inner_agent.as_tool(
        tool_name="lookup",
        tool_description="Look up a value with the inner agent.",
    )
    outer_agent = Agent(
        name="outer",
        model=FakeModel(
            initial_output=[
                get_function_tool_call(
                    "lookup",
                    '{"input":"hi"}',
                    call_id="call_lookup",
                )
            ]
        ),
        tools=[nested_tool],
    )

    initial_result = await Runner.run(outer_agent, "Look this up")
    state = await RunState.from_json(outer_agent, initial_result.to_state().to_json())
    state.approve(state.get_interruptions()[0])
    outer_agent.tools = [
        function_tool(
            lambda input: _record(calls, input, "local"),
            name_override="lookup",
        )
    ]

    with pytest.raises(
        ModelBehaviorError,
        match="Cannot reconcile queued tool lookup with a new tool",
    ):
        await Runner.run(outer_agent, state)

    assert calls == []


@pytest.mark.asyncio
async def test_resume_preserves_model_order_for_function_outcomes() -> None:
    calls: list[str] = []
    missing_tool = function_tool(
        lambda: _record(calls, "missing"),
        name_override="missing_lookup",
        needs_approval=True,
    )
    rejected_tool = function_tool(
        lambda: _record(calls, "rejected"),
        name_override="rejected_lookup",
        needs_approval=True,
    )
    available_tool = function_tool(
        lambda: _record(calls, "available"),
        name_override="available_lookup",
        needs_approval=True,
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("missing_lookup", "{}", call_id="missing_call"),
            get_function_tool_call("rejected_lookup", "{}", call_id="rejected_call"),
            get_function_tool_call("available_lookup", "{}", call_id="available_call"),
        ]
    )
    agent = Agent(
        name="agent",
        model=model,
        tools=[missing_tool, rejected_tool, available_tool],
    )

    initial_result = await Runner.run(agent, "Look these up")
    state = await RunState.from_json(agent, initial_result.to_state().to_json())
    for interruption in state.get_interruptions():
        if interruption.tool_name == "rejected_lookup":
            state.reject(interruption)
        else:
            state.approve(interruption)
    agent.tools = [rejected_tool, available_tool]
    model.set_next_output([get_text_message("done")])

    resumed_result = await Runner.run(
        agent,
        state,
        run_config=RunConfig(tool_not_found_behavior="return_error_to_model"),
    )

    assert resumed_result.final_output == "done"
    assert calls == ["available"]
    output_ids = [
        cast(dict[str, Any], item.raw_item)["call_id"]
        for item in resumed_result.new_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "function_call_output"
    ]
    assert output_ids == ["missing_call", "rejected_call", "available_call"]


@pytest.mark.asyncio
async def test_resume_preserves_multiple_agent_tool_calls() -> None:
    inner_calls: list[str] = []

    @function_tool(needs_approval=True)
    async def inner_hitl_tool() -> str:
        inner_calls.append("inner")
        return "ok"

    inner_model = FakeModel()
    inner_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call(inner_hitl_tool.name, "{}", call_id="inner-1")],
            [get_function_tool_call(inner_hitl_tool.name, "{}", call_id="inner-2")],
            [get_text_message("inner done")],
            [get_text_message("inner done")],
        ]
    )
    inner_agent = Agent(name="inner", model=inner_model, tools=[inner_hitl_tool])
    agent_tool = inner_agent.as_tool(
        tool_name="inner_agent_tool",
        tool_description="Run the inner agent.",
        needs_approval=False,
    )
    outer_model = FakeModel(
        initial_output=[
            get_function_tool_call(
                agent_tool.name,
                '{"input":"a"}',
                call_id="outer-a",
            ),
            get_function_tool_call(
                agent_tool.name,
                '{"input":"b"}',
                call_id="outer-b",
            ),
        ]
    )
    outer_agent = Agent(name="outer", model=outer_model, tools=[agent_tool])
    initial_result = await Runner.run(outer_agent, "start")
    state = initial_result.to_state()
    for interruption in state.get_interruptions():
        state.approve(interruption)
    outer_model.set_next_output([get_text_message("done")])

    resumed_result = await Runner.run(outer_agent, state)

    assert resumed_result.final_output == "done"
    assert inner_calls == ["inner", "inner"]
    outer_outputs = [
        item
        for item in resumed_result.new_items
        if isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "function_call_output"
        and item.raw_item.get("call_id") in {"outer-a", "outer-b"}
    ]
    assert len(outer_outputs) == 2


@pytest.mark.parametrize("override_all_tools", [False, True])
@pytest.mark.asyncio
async def test_resume_reuses_handoff_snapshot_for_delegating_overrides(
    override_all_tools: bool,
) -> None:
    resume_phase = False
    resume_enablement_checks: list[bool] = []
    calls: list[str] = []
    server = FakeMCPServer()
    server.add_tool("search", {"type": "object", "properties": {}})

    def handoff_enabled(
        _context: RunContextWrapper[None],
        _agent: Agent[None],
    ) -> bool:
        if not resume_phase:
            return True
        enabled = not resume_enablement_checks
        resume_enablement_checks.append(enabled)
        return enabled

    approval_tool = function_tool(
        lambda: _record(calls, "approved"),
        name_override="approval_tool",
        needs_approval=True,
    )
    target = Agent(
        name="target",
        model=FakeModel(initial_output=[get_text_message("done")]),
    )
    route_handoff = handoff(
        target,
        tool_name_override="route",
        on_handoff=lambda _: calls.append("handoff"),
        is_enabled=handoff_enabled,
    )

    class DelegatingMCPAgent(Agent[None]):
        async def get_mcp_tools(
            self,
            run_context: RunContextWrapper[None],
        ) -> list[Tool]:
            return await super().get_mcp_tools(run_context)

    class DelegatingAllToolsAgent(Agent[None]):
        async def get_all_tools(
            self,
            run_context: RunContextWrapper[None],
        ) -> list[Tool]:
            return await super().get_all_tools(run_context)

    model = FakeModel(
        initial_output=[
            get_function_tool_call("approval_tool", "{}", call_id="approval_call"),
            get_handoff_tool_call(target, override_name="route", args="{}"),
        ]
    )
    agent_class = DelegatingAllToolsAgent if override_all_tools else DelegatingMCPAgent
    agent = agent_class(
        name="agent",
        model=model,
        tools=[approval_tool],
        handoffs=[route_handoff],
        mcp_servers=[server],
        mcp_config={"include_server_in_tool_names": True},
    )

    initial_result = await Runner.run(agent, "Route this request")
    state = initial_result.to_state()
    state.approve(state.get_interruptions()[0])
    resume_phase = True

    resumed_result = await Runner.run(agent, state)

    assert resumed_result.final_output == "done"
    assert resume_enablement_checks == [True]
    assert calls == ["approved", "handoff"]


@pytest.mark.asyncio
async def test_resume_rejects_conflicting_persisted_identity_before_sibling_effects() -> None:
    calls: list[str] = []
    conflicting_tool = function_tool(
        lambda: _record(calls, "conflicting"),
        name_override="lookup",
        needs_approval=True,
    )
    sibling_tool = function_tool(
        lambda: _record(calls, "sibling"),
        name_override="sibling",
        needs_approval=True,
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("lookup", "{}", call_id="conflicting_call"),
            get_function_tool_call("sibling", "{}", call_id="sibling_call"),
        ]
    )
    agent = Agent(name="agent", model=model, tools=[conflicting_tool, sibling_tool])

    initial_result = await Runner.run(agent, "Look these up")
    state = initial_result.to_state()
    interruptions = state.get_interruptions()
    for interruption in interruptions:
        state.approve(interruption)
    conflicting = next(item for item in interruptions if item.call_id == "conflicting_call")
    conflicting.raw_item = {
        "type": "function_call",
        "name": "lookup",
        "namespace": "current",
        "call_id": "conflicting_call",
    }
    conflicting.tool_lookup_key = ("namespaced", "legacy", "lookup")

    with pytest.raises(ModelBehaviorError, match="Persisted tool identity"):
        await Runner.run(agent, state)

    assert calls == []


@pytest.mark.asyncio
async def test_resume_rejects_legacy_approval_name_change_before_side_effects() -> None:
    calls: list[str] = []
    old_tool = function_tool(
        lambda: _record(calls, "old"),
        name_override="old_lookup",
        needs_approval=True,
    )
    new_tool = function_tool(
        lambda: _record(calls, "new"),
        name_override="new_lookup",
    )
    model = FakeModel(
        initial_output=[get_function_tool_call("old_lookup", "{}", call_id="lookup_call")]
    )
    agent = Agent(name="agent", model=model, tools=[old_tool])

    initial_result = await Runner.run(agent, "Look this up")
    state = initial_result.to_state()
    interruption = state.get_interruptions()[0]
    state.approve(interruption)
    interruption.tool_lookup_key = None
    interruption.raw_item = {
        "type": "function_call",
        "name": "new_lookup",
        "arguments": "{}",
        "call_id": "lookup_call",
    }
    agent.tools = [new_tool]

    with pytest.raises(ModelBehaviorError, match="Persisted tool identity"):
        await Runner.run(agent, state)

    assert calls == []


@pytest.mark.parametrize("namespace", [None, "tools"])
@pytest.mark.parametrize("return_error_to_model", [False, True])
@pytest.mark.asyncio
async def test_resume_treats_apply_patch_prefixed_queued_function_as_function(
    namespace: str | None,
    return_error_to_model: bool,
) -> None:
    calls: list[str] = []
    base_tool = function_tool(
        lambda: _record(calls, "function"),
        name_override="apply_patch_lookup",
        needs_approval=True,
    )
    tools: list[Tool] = []
    if namespace is not None:
        tools.extend(tool_namespace(name=namespace, description="Lookup tools", tools=[base_tool]))
    else:
        tools.append(base_tool)
    model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "apply_patch_lookup",
                "{}",
                call_id="lookup_call",
                namespace=namespace,
            )
        ]
    )
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=tools)

    initial_result = await Runner.run(agent, "Look this up")
    state = initial_result.to_state()
    state.approve(state.get_interruptions()[0])
    agent.tools = []
    run_config = RunConfig(
        tool_not_found_behavior=(
            "return_error_to_model" if return_error_to_model else "raise_error"
        )
    )

    if return_error_to_model:
        resumed_result = await Runner.run(agent, state, run_config=run_config)
        assert resumed_result.final_output == "done"
    else:
        with pytest.raises(
            ModelBehaviorError,
            match=r"Tool .*apply_patch_lookup not found in agent agent",
        ):
            await Runner.run(agent, state, run_config=run_config)

    assert calls == []


@pytest.mark.parametrize(
    "malformed_raw_item",
    [
        {
            "type": "function_call",
            "name": "lookup",
            "call_id": "lookup_call",
        },
        {
            "type": "function_call",
            "name": "lookup",
            "arguments": "{}",
            "id": "lookup_call",
        },
    ],
)
@pytest.mark.asyncio
async def test_approved_malformed_approval_only_stays_pending_without_side_effects(
    malformed_raw_item: dict[str, Any],
) -> None:
    calls: list[str] = []
    tool = function_tool(
        lambda: _record(calls, "lookup"),
        name_override="lookup",
        needs_approval=True,
    )
    model = FakeModel(
        initial_output=[get_function_tool_call("lookup", "{}", call_id="lookup_call")]
    )
    agent = Agent(name="agent", model=model, tools=[tool])

    initial_result = await Runner.run(agent, "Look this up")
    state = initial_result.to_state()
    interruption = state.get_interruptions()[0]
    state.approve(interruption)
    assert state._last_processed_response is not None
    state._last_processed_response.functions = []
    state._model_responses[-1] = replace(state._model_responses[-1], output=[])
    interruption.raw_item = malformed_raw_item

    resumed_result = await Runner.run(agent, state)

    assert len(resumed_result.interruptions) == 1
    assert resumed_result.interruptions[0].call_id == "lookup_call"
    assert calls == []


@pytest.mark.asyncio
async def test_resume_snapshots_function_approval_before_tool_inventory_await() -> None:
    calls: list[str] = []
    approval_holder: dict[str, Any] = {}

    lookup_tool = function_tool(
        lambda: _record(calls, "lookup"),
        name_override="lookup",
        needs_approval=True,
    )
    other_tool = function_tool(
        lambda: _record(calls, "other"),
        name_override="other",
    )

    class MutatingAgent(Agent[None]):
        async def get_all_tools(
            self,
            run_context: RunContextWrapper[None],
        ) -> list[Tool]:
            approval = approval_holder.get("approval")
            if approval is not None:
                approval.tool_name = "other"
                approval.tool_namespace = None
                approval.tool_origin = "local"
                approval.tool_lookup_key = ("bare", "other")
                approval._allow_bare_name_alias = True
                approval.raw_item.name = "other"
            return await super().get_all_tools(run_context)

    model = FakeModel(
        initial_output=[get_function_tool_call("lookup", "{}", call_id="lookup_call")]
    )
    model.set_next_output([get_text_message("done")])
    agent = MutatingAgent(name="agent", model=model, tools=[lookup_tool, other_tool])

    initial_result = await Runner.run(agent, "Look this up")
    state = initial_result.to_state()
    approval = state.get_interruptions()[0]
    state.approve(approval)
    approval_holder["approval"] = approval

    resumed_result = await Runner.run(agent, state)

    assert resumed_result.final_output == "done"
    assert calls == ["lookup"]


@pytest.mark.asyncio
async def test_resume_uses_queued_arguments_instead_of_mutated_approval_arguments() -> None:
    calls: list[int] = []

    def lookup(amount: int) -> str:
        calls.append(amount)
        return str(amount)

    lookup_tool = function_tool(
        lookup,
        name_override="lookup",
        needs_approval=True,
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "lookup",
                '{"amount":10}',
                call_id="lookup_call",
            )
        ]
    )
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[lookup_tool])

    initial_result = await Runner.run(agent, "Look this up")
    state = await RunState.from_json(agent, initial_result.to_state().to_json())
    approval = state.get_interruptions()[0]
    state.approve(approval)
    cast(Any, approval.raw_item).arguments = '{"amount":999}'

    resumed_result = await Runner.run(agent, state)

    assert resumed_result.final_output == "done"
    assert calls == [10]


@pytest.mark.asyncio
async def test_resume_deep_copies_approval_only_program_caller_before_inventory_await() -> None:
    calls: list[str] = []
    approval_holder: dict[str, Any] = {}
    program = Program(
        id="program_item",
        call_id="program_call",
        code="lookup()",
        fingerprint="fingerprint",
        type="program",
    )
    function_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call("lookup", "{}", call_id="lookup_call"),
    )
    function_call.caller = CallerProgram(type="program", caller_id="program_call")

    lookup_tool = function_tool(
        lambda: _record(calls, "lookup"),
        name_override="lookup",
        needs_approval=True,
        allowed_callers=["programmatic"],
    )

    class MutatingCallerAgent(Agent[None]):
        async def get_all_tools(
            self,
            run_context: RunContextWrapper[None],
        ) -> list[Tool]:
            approval = approval_holder.get("approval")
            if approval is not None:
                cast(Any, approval.raw_item).caller.caller_id = "mutated_program"
            return await super().get_all_tools(run_context)

    model = FakeModel(initial_output=[program, function_call])
    model.set_next_output([get_text_message("done")])
    agent = MutatingCallerAgent(
        name="agent",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_tool],
    )

    initial_result = await Runner.run(agent, "Look this up")
    state = initial_result.to_state()
    approval = state.get_interruptions()[0]
    state.approve(approval)
    assert state._last_processed_response is not None
    state._last_processed_response.functions = []
    state._model_responses[-1] = replace(
        state._model_responses[-1],
        output=[program],
    )
    approval_holder["approval"] = approval

    resumed_result = await Runner.run(agent, state)

    assert resumed_result.final_output == "done"
    assert calls == ["lookup"]


@pytest.mark.asyncio
async def test_resume_snapshots_program_parent_context_before_inventory_await() -> None:
    calls: list[str] = []
    mutate_parent = False
    program = Program(
        id="program_item",
        call_id="legit_program",
        code="lookup()",
        fingerprint="fingerprint",
        type="program",
    )
    function_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call("lookup", "{}", call_id="lookup_call"),
    )
    function_call.caller = CallerProgram(type="program", caller_id="legit_program")
    lookup_tool = function_tool(
        lambda: _record(calls, "lookup"),
        name_override="lookup",
        needs_approval=True,
        allowed_callers=["programmatic"],
    )

    class MutatingParentAgent(Agent[None]):
        async def get_all_tools(
            self,
            run_context: RunContextWrapper[None],
        ) -> list[Tool]:
            if mutate_parent:
                program.call_id = "forged_program"
            return await super().get_all_tools(run_context)

    model = FakeModel(initial_output=[program, function_call])
    agent = MutatingParentAgent(
        name="agent",
        model=model,
        tools=[ProgrammaticToolCallingTool(), lookup_tool],
    )

    initial_result = await Runner.run(agent, "Look this up")
    state = initial_result.to_state()
    approval = state.get_interruptions()[0]
    state.approve(approval)
    cast(Any, approval.raw_item).caller.caller_id = "forged_program"
    assert state._last_processed_response is not None
    state._last_processed_response.functions = []
    state._model_responses[-1] = replace(
        state._model_responses[-1],
        output=[program],
    )
    mutate_parent = True

    with pytest.raises(ModelBehaviorError, match="does not match a parent program item"):
        await Runner.run(agent, state)

    assert calls == []


@pytest.mark.asyncio
async def test_resume_rejects_response_backed_approval_lookup_mismatch_before_effects() -> None:
    calls: list[str] = []
    lookup_tool = function_tool(
        lambda: _record(calls, "lookup"),
        name_override="lookup",
        needs_approval=True,
    )
    sibling_tool = function_tool(
        lambda: _record(calls, "sibling"),
        name_override="sibling",
        needs_approval=True,
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("lookup", "{}", call_id="lookup_call"),
            get_function_tool_call("sibling", "{}", call_id="sibling_call"),
        ]
    )
    agent = Agent(name="agent", model=model, tools=[lookup_tool, sibling_tool])

    initial_result = await Runner.run(agent, "Look these up")
    state = await RunState.from_json(agent, initial_result.to_state().to_json())
    interruptions = state.get_interruptions()
    for interruption in interruptions:
        state.approve(interruption)
    assert state._last_processed_response is not None
    state._last_processed_response.functions = [
        run
        for run in state._last_processed_response.functions
        if run.tool_call.call_id != "lookup_call"
    ]
    lookup_approval = next(item for item in interruptions if item.call_id == "lookup_call")
    cast(Any, lookup_approval.raw_item).name = "other"
    lookup_approval.tool_name = "other"
    lookup_approval.tool_lookup_key = ("bare", "other")

    resumed_result = await Runner.run(agent, state)

    assert len(resumed_result.interruptions) == 2
    assert calls == []


@pytest.mark.asyncio
async def test_resume_preserves_function_shaped_apply_patch_owner() -> None:
    operations: list[Any] = []

    class Editor:
        def create_file(self, operation: Any) -> dict[str, str]:
            operations.append(operation)
            return {"output": "created", "status": "completed"}

        def update_file(self, operation: Any) -> dict[str, str]:
            operations.append(operation)
            return {"output": "updated", "status": "completed"}

        def delete_file(self, operation: Any) -> dict[str, str]:
            operations.append(operation)
            return {"output": "deleted", "status": "completed"}

    patch_tool = ApplyPatchTool(editor=cast(Any, Editor()), needs_approval=True)
    model = FakeModel(
        initial_output=[
            get_function_tool_call(
                "apply_patch",
                '{"type":"update_file","path":"test.md","diff":"-a\\n+b\\n"}',
                call_id="patch_call",
            )
        ]
    )
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[patch_tool])

    initial_result = await Runner.run(agent, "Update the file")
    state = initial_result.to_state()
    state.approve(state.get_interruptions()[0])

    resumed_result = await Runner.run(agent, state)

    assert resumed_result.final_output == "done"
    assert len(operations) == 1


@pytest.mark.asyncio
async def test_nested_rebind_is_not_committed_before_later_strict_missing_error() -> None:
    calls: list[str] = []
    before_tool = function_tool(
        lambda: _record(calls, "before"),
        name_override="before",
    )
    sensitive_tool = function_tool(
        lambda: _record(calls, "sensitive"),
        name_override="sensitive",
        needs_approval=True,
    )
    inner_model = FakeModel(
        initial_output=[
            get_function_tool_call("before", "{}", call_id="before_call"),
            get_function_tool_call("sensitive", "{}", call_id="sensitive_call"),
        ]
    )
    inner_model.set_next_output([get_text_message("inner done")])
    inner_agent = Agent(
        name="inner",
        model=inner_model,
        tools=[before_tool, sensitive_tool],
    )
    nested_tool = inner_agent.as_tool(
        tool_name="nested",
        tool_description="Run the inner agent.",
    )
    missing_tool = function_tool(
        lambda: _record(calls, "missing"),
        name_override="missing",
        needs_approval=True,
    )
    outer_model = FakeModel(
        initial_output=[
            get_function_tool_call("nested", '{"input":"go"}', call_id="nested_call"),
            get_function_tool_call("missing", "{}", call_id="missing_call"),
        ]
    )
    outer_model.set_next_output([get_text_message("outer done")])
    outer_agent = Agent(
        name="outer",
        model=outer_model,
        tools=[nested_tool, missing_tool],
    )

    initial_result = await Runner.run(outer_agent, "Start")
    assert calls == ["before"]
    state = initial_result.to_state()
    for interruption in state.get_interruptions():
        state.approve(interruption)
    outer_agent.tools = [nested_tool]

    with pytest.raises(ModelBehaviorError, match="Tool missing not found"):
        await Runner.run(outer_agent, state)

    outer_agent.tools = [nested_tool, missing_tool]
    restored_state = await RunState.from_json(outer_agent, state.to_json())
    resumed_result = await Runner.run(outer_agent, restored_state)

    assert resumed_result.final_output == "outer done"
    assert calls[0] == "before"
    assert sorted(calls[1:]) == ["missing", "sensitive"]


@pytest.mark.asyncio
async def test_cross_kind_duplicate_call_id_fails_before_execution() -> None:
    calls: list[str] = []
    missing_tool = function_tool(
        lambda: _record(calls, "missing"),
        name_override="missing",
        needs_approval=True,
    )
    original_tool = function_tool(
        lambda: _record(calls, "original"),
        name_override="lookup",
        needs_approval=True,
    )
    shell_tool = ShellTool(
        executor=lambda _request: _record(calls, "shell"),
    )
    shell_call = cast(
        Any,
        {
            "type": "shell_call",
            "id": "shell_item",
            "call_id": "shared_call",
            "status": "completed",
            "action": {
                "type": "exec",
                "commands": ["echo test"],
                "timeout_ms": 1000,
            },
        },
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("missing", "{}", call_id="missing_call"),
            get_function_tool_call("lookup", "{}", call_id="shared_call"),
            shell_call,
        ]
    )
    agent = Agent(
        name="agent",
        model=model,
        tools=[missing_tool, original_tool, shell_tool],
    )

    with pytest.raises(ModelBehaviorError, match="unique call ID"):
        await Runner.run(agent, "Look this up")

    assert calls == []


@pytest.mark.parametrize(
    "malformed_raw_item",
    [
        {"name": "lookup"},
        {
            "type": "function_call",
            "name": "lookup",
            "arguments": "{}",
            "call_id": "changed_call",
        },
    ],
)
@pytest.mark.asyncio
async def test_approved_malformed_queued_approval_stays_pending_without_side_effects(
    malformed_raw_item: dict[str, Any],
) -> None:
    calls: list[str] = []
    lookup_tool = function_tool(
        lambda: _record(calls, "lookup"),
        name_override="lookup",
        needs_approval=True,
    )
    sibling_tool = function_tool(
        lambda: _record(calls, "sibling"),
        name_override="sibling",
        needs_approval=True,
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("lookup", "{}", call_id="lookup_call"),
            get_function_tool_call("sibling", "{}", call_id="sibling_call"),
        ]
    )
    agent = Agent(name="agent", model=model, tools=[lookup_tool, sibling_tool])

    initial_result = await Runner.run(agent, "Look these up")
    state = initial_result.to_state()
    interruptions = state.get_interruptions()
    for interruption in interruptions:
        state.approve(interruption)
    lookup_approval = next(item for item in interruptions if item.call_id == "lookup_call")
    lookup_approval.raw_item = malformed_raw_item

    resumed_result = await Runner.run(agent, state)

    assert lookup_approval in resumed_result.interruptions
    assert calls == []


@pytest.mark.asyncio
async def test_resume_rejects_cross_kind_approval_identity_before_sibling_effects() -> None:
    calls: list[str] = []
    lookup_tool = function_tool(
        lambda: _record(calls, "lookup"),
        name_override="lookup",
        needs_approval=True,
    )
    sibling_tool = function_tool(
        lambda: _record(calls, "sibling"),
        name_override="sibling",
        needs_approval=True,
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("lookup", "{}", call_id="lookup_call"),
            get_function_tool_call("sibling", "{}", call_id="sibling_call"),
        ]
    )
    agent = Agent(name="agent", model=model, tools=[lookup_tool, sibling_tool])

    initial_result = await Runner.run(agent, "Look these up")
    state = initial_result.to_state()
    interruptions = state.get_interruptions()
    for interruption in interruptions:
        state.approve(interruption)
    lookup_approval = next(item for item in interruptions if item.call_id == "lookup_call")
    lookup_approval.raw_item = {
        "type": "custom_tool_call",
        "name": "evil",
        "call_id": "lookup_call",
        "input": "{}",
    }

    with pytest.raises(ModelBehaviorError, match="Persisted tool identity"):
        await Runner.run(agent, state)

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["non-streamed", "streamed"])
async def test_missing_formatter_cancellation_precedes_sibling_side_effects(
    streamed: bool,
) -> None:
    calls: list[str] = []
    formatter_started = asyncio.Event()
    keep_formatter_waiting = asyncio.Event()
    missing_tool = function_tool(
        lambda: _record(calls, "missing"),
        name_override="missing",
        needs_approval=True,
    )
    available_tool = function_tool(
        lambda: _record(calls, "available"),
        name_override="available",
        needs_approval=True,
    )
    model = FakeModel(
        initial_output=[
            get_function_tool_call("missing", "{}", call_id="missing_call"),
            get_function_tool_call("available", "{}", call_id="available_call"),
        ]
    )
    model.set_next_output([get_text_message("done")])
    agent = Agent(name="agent", model=model, tools=[missing_tool, available_tool])

    initial_result = await Runner.run(agent, "Look these up")
    state = initial_result.to_state()
    for interruption in state.get_interruptions():
        state.approve(interruption)
    agent.tools = [available_tool]

    async def blocking_formatter(_args: Any) -> str:
        formatter_started.set()
        await keep_formatter_waiting.wait()
        return "missing"

    async def resume(run_config: RunConfig) -> Any:
        if not streamed:
            return await Runner.run(agent, state, run_config=run_config)
        result = Runner.run_streamed(agent, state, run_config=run_config)
        async for _event in result.stream_events():
            pass
        return result

    resume_task = asyncio.create_task(
        resume(
            RunConfig(
                tool_not_found_behavior="return_error_to_model",
                tool_error_formatter=blocking_formatter,
            )
        )
    )
    await formatter_started.wait()
    resume_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resume_task

    assert calls == []

    with pytest.raises(ModelBehaviorError, match="already executed"):
        await resume(RunConfig(tool_not_found_behavior="return_error_to_model"))

    assert calls == []
