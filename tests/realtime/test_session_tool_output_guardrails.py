"""Function tool output policies at the Realtime session dispatch boundary."""

import asyncio
import json
from typing import Any, Literal

import pytest

from agents.decorators import tool, tool_output_guardrail
from agents.exceptions import ModelBehaviorError, ToolOutputGuardrailTripwireTriggered
from agents.realtime import RealtimeAgent, RealtimeSession
from agents.realtime.events import RealtimeSessionEvent, RealtimeToolEnd
from agents.realtime.model_events import RealtimeModelToolCallEvent
from agents.realtime.model_inputs import RealtimeModelSendEvent, RealtimeModelSendToolOutput
from agents.realtime.testing import ScriptedRealtimeModel
from agents.tool_context import ToolContext
from agents.tool_guardrails import ToolGuardrailFunctionOutput, ToolOutputGuardrailData

from ..mcp.helpers import FakeMCPServer


def tool_call() -> RealtimeModelToolCallEvent:
    return RealtimeModelToolCallEvent(name="lookup", call_id="call_lookup", arguments="{}")


def outputs(model: ScriptedRealtimeModel) -> list[str]:
    return [
        event.output
        for event in model.sent_events
        if isinstance(event, RealtimeModelSendToolOutput)
    ]


async def collect_events(session: RealtimeSession, events: list[RealtimeSessionEvent]) -> None:
    async for event in session:
        events.append(event)


async def next_event(session: RealtimeSession, event_type: str) -> RealtimeSessionEvent:
    async def wait() -> RealtimeSessionEvent:
        async for event in session:
            if event.type == event_type:
                return event
        raise AssertionError(f"Session closed before {event_type}")

    return await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_tool_output_policies_check_original_result_in_order(reject: bool) -> None:
    original = {"value": "synthetic tool result"}
    seen: list[tuple[str, ToolOutputGuardrailData]] = []
    invocation_contexts: list[ToolContext[Any]] = []

    @tool_output_guardrail
    def first(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        seen.append(("first", data))
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    async def second(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        seen.append(("second", data))
        if reject:
            return ToolGuardrailFunctionOutput.reject_content("replacement output")
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    def third(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        seen.append(("third", data))
        return ToolGuardrailFunctionOutput.allow()

    @tool(tool_output_guardrails=[first, second, third])
    async def lookup(context: ToolContext[Any]) -> dict[str, str]:
        invocation_contexts.append(context)
        return original

    agent = RealtimeAgent(name="source", tools=[lookup])
    model = ScriptedRealtimeModel(strict=False)
    async with RealtimeSession(
        model, agent, {"user": "synthetic"}, run_config={"async_tool_calls": False}
    ) as session:
        await model.emit(tool_call())
        end = await next_event(session, "tool_end")
        assert isinstance(end, RealtimeToolEnd)
        assert end.output == ("replacement output" if reject else original)
        assert end.agent is agent
        assert outputs(model) == ["replacement output" if reject else json.dumps(original)]
        assert [name for name, _ in seen] == (
            ["first", "second"] if reject else ["first", "second", "third"]
        )
        for _, data in seen:
            assert data.output is original
            assert data.agent is agent
            assert data.context is invocation_contexts[0]
            assert data.context.tool_call_id == "call_lookup"
            assert data.context.tool_arguments == "{}"
        await model.emit(tool_call())
        assert len(invocation_contexts) == 1
        assert len(outputs(model)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("async_tools", [False, True])
@pytest.mark.parametrize("behavior", ["tripwire", "error"])
async def test_output_policy_failure_never_publishes_or_reexecutes(
    async_tools: bool, behavior: str
) -> None:
    calls = 0
    checks = 0
    events: list[RealtimeSessionEvent] = []

    @tool_output_guardrail
    async def policy(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        nonlocal checks
        checks += 1
        assert data.output == "unchecked result"
        if behavior == "error":
            raise ValueError("policy unavailable")
        return ToolGuardrailFunctionOutput.raise_exception()

    @tool(tool_output_guardrails=[policy])
    async def lookup() -> str:
        nonlocal calls
        calls += 1
        return "unchecked result"

    model = ScriptedRealtimeModel(strict=False)
    session = RealtimeSession(
        model,
        RealtimeAgent(name="source", tools=[lookup]),
        None,
        run_config={"async_tool_calls": async_tools},
    )
    expected_error = ValueError if behavior == "error" else ToolOutputGuardrailTripwireTriggered
    async with session:
        if async_tools:
            consumer = asyncio.create_task(collect_events(session, events))
            await model.emit(tool_call())
            with pytest.raises(expected_error):
                await asyncio.wait_for(consumer, timeout=2)
        else:
            with pytest.raises(expected_error):
                await model.emit(tool_call())
            with pytest.raises(ModelBehaviorError, match="already executed"):
                await model.emit(tool_call())
    if not async_tools:
        await collect_events(session, events)
    assert calls == checks == 1
    assert outputs(model) == []
    assert not any(isinstance(event, RealtimeToolEnd) for event in events)
    assert session._pending_tool_outputs == {}


@pytest.mark.asyncio
async def test_mcp_output_policy_is_enforced_after_conversion() -> None:
    seen: list[Any] = []

    @tool_output_guardrail
    def policy(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        seen.append(data.output)
        return ToolGuardrailFunctionOutput.reject_content("MCP replacement")

    server = FakeMCPServer(tool_output_guardrails=[policy])
    server.add_tool("lookup", {"type": "object", "properties": {}})
    agent = RealtimeAgent(name="source", mcp_servers=[server])
    model = ScriptedRealtimeModel(strict=False)
    async with RealtimeSession(
        model, agent, None, run_config={"async_tool_calls": False}
    ) as session:
        await model.emit(tool_call())
        end = await next_event(session, "tool_end")
        assert isinstance(end, RealtimeToolEnd)
        assert end.output == "MCP replacement"
        assert outputs(model) == ["MCP replacement"]
        assert server.tool_calls == ["lookup"]
        assert seen == [{"type": "text", "text": server.tool_results[0]}]


@pytest.mark.asyncio
async def test_approved_async_tool_output_policy_keeps_dispatch_agent() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    checks: list[ToolOutputGuardrailData] = []

    @tool_output_guardrail
    async def policy(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        checks.append(data)
        started.set()
        await release.wait()
        return ToolGuardrailFunctionOutput.reject_content("approved replacement")

    @tool(needs_approval=True, tool_output_guardrails=[policy])
    async def lookup() -> str:
        return "unchecked result"

    agent = RealtimeAgent(name="source", tools=[lookup])
    model = ScriptedRealtimeModel(strict=False)
    async with RealtimeSession(model, agent, None) as session:
        await model.emit(tool_call())
        await next_event(session, "tool_approval_required")
        assert checks == []
        await session.approve_tool_call("call_lookup")
        await asyncio.wait_for(started.wait(), timeout=2)
        await session.update_agent(RealtimeAgent(name="replacement"))
        assert outputs(model) == []
        release.set()
        end = await next_event(session, "tool_end")
        assert isinstance(end, RealtimeToolEnd)
        assert end.agent is agent
        assert end.output == "approved replacement"
        assert checks[0].agent is agent
        assert checks[0].context.agent is agent
        assert outputs(model) == ["approved replacement"]


class FailOnceModel(ScriptedRealtimeModel):
    def __init__(self) -> None:
        super().__init__(strict=False)
        self.attempts: list[str] = []

    async def send_event(self, event: RealtimeModelSendEvent) -> None:
        if isinstance(event, RealtimeModelSendToolOutput):
            self.attempts.append(event.output)
            if len(self.attempts) == 1:
                raise RuntimeError("transport unavailable")
        await super().send_event(event)


@pytest.mark.asyncio
async def test_transport_retry_reuses_only_checked_output() -> None:
    calls = 0
    checks = 0

    @tool_output_guardrail
    def policy(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        nonlocal checks
        checks += 1
        return ToolGuardrailFunctionOutput.reject_content("retry replacement")

    @tool(tool_output_guardrails=[policy])
    async def lookup() -> str:
        nonlocal calls
        calls += 1
        return "unchecked result"

    model = FailOnceModel()
    async with RealtimeSession(
        model, RealtimeAgent(name="source", tools=[lookup]), None
    ) as session:
        await model.emit(tool_call())
        await next_event(session, "error")
        assert outputs(model) == []
        pending = session._pending_tool_outputs["call_lookup"]
        assert pending.output == "retry replacement"
        assert pending.tool_end_event is not None
        assert pending.tool_end_event.output == "retry replacement"
        await model.emit(tool_call())
        end = await next_event(session, "tool_end")
        assert isinstance(end, RealtimeToolEnd)
        assert end.output == "retry replacement"
        assert outputs(model) == ["retry replacement"]
        assert model.attempts == ["retry replacement", "retry replacement"]
        assert calls == checks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["cancel", "close", "close_suppress_cancel"])
async def test_output_policy_shutdown_prevents_late_output(
    shutdown: Literal["cancel", "close", "close_suppress_cancel"],
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0
    events: list[RealtimeSessionEvent] = []

    @tool_output_guardrail
    async def policy(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            if shutdown != "close_suppress_cancel":
                raise
        return ToolGuardrailFunctionOutput.allow()

    @tool(tool_output_guardrails=[policy])
    async def lookup() -> str:
        nonlocal calls
        calls += 1
        return "unchecked result"

    model = ScriptedRealtimeModel(strict=False)
    async with RealtimeSession(
        model,
        RealtimeAgent(name="source", tools=[lookup]),
        None,
        run_config={"async_tool_calls": shutdown != "cancel"},
    ) as session:
        if shutdown == "cancel":
            dispatch = asyncio.create_task(session.on_event(tool_call()))
            await asyncio.wait_for(started.wait(), timeout=2)
            dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch
            with pytest.raises(ModelBehaviorError, match="already executed"):
                await session.on_event(tool_call())
        else:
            await model.emit(tool_call())
            await asyncio.wait_for(started.wait(), timeout=2)
            await session.close()
        assert cancelled.is_set()
    await collect_events(session, events)
    assert calls == 1
    assert outputs(model) == []
    assert not any(isinstance(event, RealtimeToolEnd) for event in events)
