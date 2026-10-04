from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from typing import Any

import pytest

from agents import Agent, GuardrailFunctionOutput, RunConfig, Runner
from agents.decorators import input_guardrail, tool
from agents.items import TResponseInputItem
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tracing import custom_span, get_current_span, get_current_trace, trace
from agents.tracing.provider import DefaultTraceProvider
from agents.tracing.setup import get_trace_provider, set_trace_provider
from agents.tracing.traces import NoOpTrace, Trace

from .testing_processor import (
    SPAN_PROCESSOR_TESTING,
    fetch_events,
    fetch_ordered_spans,
    fetch_traces,
)
from .utils.simple_session import SimpleListSession


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
async def test_runner_tracing_opt_out_inside_caller_span(streamed: bool, disabled: bool) -> None:
    @tool
    def lookup(city: str) -> str:
        with custom_span("inside-tool"):
            return f"synthetic weather for {city}"

    @input_guardrail
    def allow(ctx, agent, input):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    target = Agent(
        name="target",
        model=ScriptedModel([[assistant_message("done")]], emit_traces=True),
    )
    agent = Agent(
        name="start",
        model=ScriptedModel(
            [
                [function_call("lookup", {"city": "Paris"}, call_id="lookup-1")],
                [function_call("transfer_to_target", {}, call_id="handoff-1")],
            ],
            emit_traces=True,
        ),
        tools=[lookup],
        handoffs=[target],
        input_guardrails=[allow],
    )
    config = RunConfig(tracing_disabled=disabled)
    with trace("caller") as caller_trace, custom_span("caller-parent") as caller_span:
        with custom_span("before"):
            pass
        if streamed:
            result = Runner.run_streamed(agent, "hi", run_config=config)
            assert get_current_trace() is caller_trace
            assert get_current_span() is caller_span
            async for _ in result.stream_events():
                assert get_current_trace() is caller_trace
                assert get_current_span() is caller_span
        else:
            result = await Runner.run(agent, "hi", run_config=config)
        assert result.final_output == "done"
        assert get_current_trace() is caller_trace
        assert get_current_span() is caller_span
        with custom_span("after"):
            pass
    spans = fetch_ordered_spans()
    names = [getattr(span.span_data, "name", None) for span in spans]
    assert fetch_traces() == [caller_trace]
    if disabled:
        assert names == ["caller-parent", "before", "after"]
        assert fetch_events().count("span_start") == 3
        assert fetch_events().count("span_end") == 3
    else:
        assert {"agent", "function", "guardrail", "handoff", "generation"} <= {
            span.span_data.type for span in spans
        }
        assert "inside-tool" in names
    assert all(span.trace_id == caller_trace.trace_id for span in spans)
    assert next(
        span for span in spans if getattr(span.span_data, "name", None) == "after"
    ).parent_id == (caller_span.span_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_disabled_runner_restores_context_after_error(streamed: bool) -> None:
    agent = Agent(
        name="error", model=ScriptedModel([ModelStep.raise_error(ValueError("synthetic"))])
    )
    with trace("caller") as caller_trace, custom_span("caller-parent") as caller_span:
        with pytest.raises(ValueError, match="synthetic"):
            if streamed:
                result = Runner.run_streamed(
                    agent, "hi", run_config=RunConfig(tracing_disabled=True)
                )
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "hi", run_config=RunConfig(tracing_disabled=True))
        assert get_current_trace() is caller_trace
        assert get_current_span() is caller_span
        with custom_span("after-error"):
            pass
    assert [getattr(span.span_data, "name", None) for span in fetch_ordered_spans()] == [
        "caller-parent",
        "after-error",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_disabled_runner_cancellation_preserves_concurrent_caller(streamed: bool) -> None:
    entered = asyncio.Event()
    cleaned_up = asyncio.Event()

    @tool
    async def block() -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            with custom_span("tool-cleanup"):
                cleaned_up.set()
        return "unreachable"

    agent = Agent(
        name="blocked",
        model=ScriptedModel([[function_call("block", {}, call_id="block-1")]]),
        tools=[block],
    )
    config = RunConfig(tracing_disabled=True)
    with trace("caller") as caller_trace, custom_span("caller-parent") as caller_span:
        if streamed:
            result = Runner.run_streamed(agent, "hi", run_config=config)
        else:
            task = asyncio.create_task(Runner.run(agent, "hi", run_config=config))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert get_current_trace() is caller_trace
        assert get_current_span() is caller_span
        with custom_span("while-running"):
            pass
        if streamed:
            result.cancel()
            async for _ in result.stream_events():
                pass
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(cleaned_up.wait(), timeout=5)
        assert get_current_trace() is caller_trace
        assert get_current_span() is caller_span
    assert [getattr(span.span_data, "name", None) for span in fetch_ordered_spans()] == [
        "caller-parent",
        "while-running",
    ]


def test_disabled_runner_sync_preserves_caller_context() -> None:
    agent = Agent(name="sync", model=ScriptedModel([[assistant_message("done")]]))
    with trace("caller") as caller_trace, custom_span("caller-parent") as caller_span:
        result = Runner.run_sync(agent, "hi", run_config=RunConfig(tracing_disabled=True))
        assert result.final_output == "done"
        assert get_current_trace() is caller_trace
        assert get_current_span() is caller_span
    assert [getattr(span.span_data, "name", None) for span in fetch_ordered_spans()] == [
        "caller-parent"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_disabled_resume_does_not_inherit_caller_trace(streamed: bool) -> None:
    @tool(needs_approval=True)
    def approved_tool() -> str:
        return "synthetic result"

    agent = Agent(
        name="approval",
        tools=[approved_tool],
        model=ScriptedModel(
            [
                [function_call("approved_tool", {}, call_id="approval-1")],
                [assistant_message("done")],
            ]
        ),
    )
    interrupted = await Runner.run(agent, "hi")
    state = interrupted.to_state()
    state.approve(interrupted.interruptions[0])
    SPAN_PROCESSOR_TESTING.clear()
    with trace("caller") as caller_trace, custom_span("caller-parent") as caller_span:
        if streamed:
            result = Runner.run_streamed(agent, state, run_config=RunConfig(tracing_disabled=True))
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, state, run_config=RunConfig(tracing_disabled=True))
        assert result.final_output == "done"
        assert get_current_trace() is caller_trace
        assert get_current_span() is caller_span
        assert result.to_state()._trace_state is None
    assert fetch_traces() == [caller_trace]
    assert [getattr(span.span_data, "name", None) for span in fetch_ordered_spans()] == [
        "caller-parent"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_disabled_runner_uses_configured_provider(streamed: bool) -> None:
    disabled_context: ContextVar[bool] = ContextVar("runner_disabled", default=False)
    restored: list[bool] = []

    class DisabledTrace(NoOpTrace):
        token: Token[bool] | None = None

        def start(self, mark_as_current: bool = False) -> None:
            super().start(mark_as_current)
            if mark_as_current:
                self.token = disabled_context.set(True)

        def finish(self, reset_current: bool = False) -> None:
            try:
                super().finish(reset_current)
            finally:
                if reset_current and self.token is not None:
                    disabled_context.reset(self.token)
                    self.token = None
                    restored.append(disabled_context.get())

    class Provider(DefaultTraceProvider):
        def create_trace(self, *args: Any, **kwargs: Any) -> Trace:
            if kwargs.get("disabled"):
                return DisabledTrace()
            return super().create_trace(*args, **kwargs)

    @tool(failure_error_function=None)
    def check_context() -> str:
        assert disabled_context.get()
        assert get_current_span() is not caller_span
        return "checked"

    agent = Agent(
        name="provider",
        tools=[check_context],
        model=ScriptedModel(
            [[function_call("check_context", {}, call_id="context-1")], [assistant_message("done")]]
        ),
    )
    original_provider = get_trace_provider()
    provider = Provider()
    provider.set_processors([SPAN_PROCESSOR_TESTING])
    set_trace_provider(provider)
    try:
        with trace("caller"), custom_span("caller-parent") as caller_span:
            if streamed:
                result = Runner.run_streamed(
                    agent, "hi", run_config=RunConfig(tracing_disabled=True)
                )
                assert not disabled_context.get()
                async for _ in result.stream_events():
                    assert not disabled_context.get()
            else:
                result = await Runner.run(agent, "hi", run_config=RunConfig(tracing_disabled=True))
            assert result.final_output == "done"
            assert not disabled_context.get()
            assert restored == [False]
        assert [getattr(span.span_data, "name", None) for span in fetch_ordered_spans()] == [
            "caller-parent"
        ]
    finally:
        set_trace_provider(original_provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("failure", [None, "session-read", "input-callback"])
async def test_runner_tracing_opt_out_during_session_preparation(
    disabled: bool, failure: str | None
) -> None:
    calls: list[str] = []

    class Session(SimpleListSession):
        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            with custom_span("session-read"):
                calls.append("session-read")
                if failure == "session-read":
                    raise ValueError("synthetic session-read failure")
                return await super().get_items(limit)

    async def prepare_input(
        history: list[TResponseInputItem], new_input: list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        with custom_span("input-callback"):
            calls.append("input-callback")
            if failure == "input-callback":
                raise ValueError("synthetic input-callback failure")
            return history + new_input

    session = Session()
    agent = Agent(name="session", model=ScriptedModel([[assistant_message("done")]]))
    config = RunConfig(tracing_disabled=disabled, session_input_callback=prepare_input)
    with trace("caller") as caller_trace, custom_span("caller-parent") as caller_span:
        if failure is not None:
            with pytest.raises(ValueError, match=f"synthetic {failure} failure"):
                await Runner.run(agent, "hi", session=session, run_config=config)
            assert session.saved_items == []
        else:
            result = await Runner.run(agent, "hi", session=session, run_config=config)
            assert result.final_output == "done"
            assert session.saved_items[0] == {"role": "user", "content": "hi"}
        assert calls == (
            ["session-read"] if failure == "session-read" else ["session-read", "input-callback"]
        )
        assert get_current_trace() is caller_trace
        assert get_current_span() is caller_span
        with custom_span("after-preparation"):
            pass
    spans = fetch_ordered_spans()
    names = [getattr(span.span_data, "name", None) for span in spans]
    assert fetch_traces() == [caller_trace]
    if disabled:
        assert names == ["caller-parent", "after-preparation"]
    else:
        assert all(name in names for name in calls)
        assert all(
            span.parent_id == caller_span.span_id
            for span in spans
            if getattr(span.span_data, "name", None) in calls
        )
    assert spans[-1].parent_id == caller_span.span_id
