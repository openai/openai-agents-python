"""Input rejection stops speculative tools before sibling guardrail cleanup finishes."""

from __future__ import annotations

import asyncio

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunConfig,
    RunHooks,
    Runner,
    ToolExecutionConfig,
    ToolGuardrailFunctionOutput,
    UserError,
    input_guardrail,
)
from agents.decorators import tool, tool_input_guardrail
from agents.testing import ScriptedModel, assistant_message, function_call


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_input_rejection_cancels_pending_end_hook_once(streaming: bool):
    active_started = asyncio.Event()
    teardown_started = asyncio.Event()
    tool_cleanup_started = asyncio.Event()
    release_tool_cleanup = asyncio.Event()
    tool_cleanup_finished = asyncio.Event()
    repeated_cancellation = asyncio.Event()
    hook_started = asyncio.Event()
    hook_cancelled = asyncio.Event()
    release_hook = asyncio.Event()
    guardrail_cleanup_started = asyncio.Event()
    release_guardrail_cleanup = asyncio.Event()

    @tool
    async def active() -> str:
        active_started.set()
        await asyncio.Event().wait()
        return "unexpected"

    @tool
    async def tearing_down() -> str:
        teardown_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            tool_cleanup_started.set()
            try:
                await release_tool_cleanup.wait()
            except asyncio.CancelledError:
                repeated_cancellation.set()
                raise
            finally:
                tool_cleanup_finished.set()
            raise
        raise AssertionError("Expected tool cancellation")

    @tool
    async def completed() -> str:
        return "completed result"

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool.name == "completed":
                hook_started.set()
                try:
                    await release_hook.wait()
                except asyncio.CancelledError:
                    hook_cancelled.set()
                    raise

    @input_guardrail
    async def reject(context, agent, input):
        await active_started.wait()
        await teardown_started.wait()
        await hook_started.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    @input_guardrail
    async def sibling(context, agent, input):
        try:
            await asyncio.Event().wait()
        finally:
            guardrail_cleanup_started.set()
            await release_guardrail_cleanup.wait()
        raise AssertionError("Expected guardrail cancellation")

    agent = Agent(
        name="synthetic",
        tools=[active, tearing_down, completed],
        input_guardrails=[reject, sibling],
        model=ScriptedModel(
            [
                [
                    function_call(name, {}, call_id=name)
                    for name in ("active", "tearing_down", "completed")
                ]
            ]
        ),
    )

    async def run():
        config = RunConfig(tracing_disabled=True)
        if streaming:
            result = Runner.run_streamed(agent, "synthetic input", hooks=Hooks(), run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "synthetic input", hooks=Hooks(), run_config=config)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(guardrail_cleanup_started.wait(), 2)
        await asyncio.wait_for(tool_cleanup_started.wait(), 2)
        # A completed cancelled handler wakes the batch while both cleanups stay suspended.
        await asyncio.wait_for(hook_cancelled.wait(), 2)
        assert not repeated_cancellation.is_set()
        release_guardrail_cleanup.set()
        with pytest.raises(InputGuardrailTripwireTriggered):
            await asyncio.wait_for(task, 2)
        assert hook_cancelled.is_set()
        assert not repeated_cancellation.is_set()
    finally:
        release_hook.set()
        release_tool_cleanup.set()
        release_guardrail_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(tool_cleanup_finished.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("verdict_kind", ["pass", "reject", "reject_return", "reject_error"])
async def test_input_verdict_stops_active_and_queued_tools(streaming: bool, verdict_kind: str):
    reject = verdict_kind != "pass"
    active_started = asyncio.Event()
    active_cancelled = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    release_active = asyncio.Event()
    queued_started = asyncio.Event()

    @tool(failure_error_function=None)
    async def active() -> str:
        active_started.set()
        try:
            await release_active.wait()
        except asyncio.CancelledError:
            active_cancelled.set()
            if verdict_kind == "reject_error":
                raise ValueError("secondary cancellation cleanup failure") from None
            if verdict_kind != "reject_return":
                raise
        return "active result"

    @tool
    async def queued() -> str:
        queued_started.set()
        return "queued result"

    @input_guardrail
    async def verdict(context, agent, input):
        await active_started.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=reject)

    @input_guardrail
    async def sibling(context, agent, input):
        if not reject:
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
        raise AssertionError("Expected guardrail cancellation")

    agent = Agent(
        name="synthetic",
        tools=[active, queued],
        input_guardrails=[verdict, sibling],
        model=ScriptedModel(
            [
                [
                    function_call("active", {}, call_id="active"),
                    function_call("queued", {}, call_id="queued"),
                ],
                [assistant_message("done")],
            ]
        ),
    )

    async def run():
        config = RunConfig(
            tracing_disabled=True,
            tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1),
        )
        if streaming:
            result = Runner.run_streamed(agent, "synthetic input", run_config=config)
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(agent, "synthetic input", run_config=config)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(active_started.wait(), 2)
        if reject:
            await asyncio.wait_for(cleanup_started.wait(), 2)
            # The guardrail sibling stays suspended while cancellation reaches the tool.
            await asyncio.wait_for(active_cancelled.wait(), 2)
            assert not queued_started.is_set()
            release_active.set()
            release_cleanup.set()
            with pytest.raises(InputGuardrailTripwireTriggered):
                await asyncio.wait_for(task, 2)
            assert not queued_started.is_set()
        else:
            release_active.set()
            result = await asyncio.wait_for(task, 2)
            assert result.final_output == "done"
            assert queued_started.is_set()
            assert not active_cancelled.is_set()
            assert [
                item.raw_item["output"]
                for item in result.new_items
                if item.type == "tool_call_output_item"
            ] == ["active result", "queued result"]
    finally:
        release_active.set()
        release_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_rejection_during_tool_failure_cleanup_preserves_primary_error(streaming: bool):
    started = asyncio.Event()
    tool_cleanup_started = asyncio.Event()
    verdict_recorded = asyncio.Event()
    release_tool_cleanup = asyncio.Event()
    completed = asyncio.Event()
    repeated_cancellation = asyncio.Event()

    @tool
    async def accepted() -> str:
        return "completed result"

    @tool(failure_error_function=None)
    async def failed() -> str:
        await started.wait()
        await completed.wait()
        raise ValueError("primary synthetic failure")

    @tool
    async def pending() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            tool_cleanup_started.set()
            try:
                await release_tool_cleanup.wait()
            except asyncio.CancelledError:
                repeated_cancellation.set()
                raise
            raise
        raise AssertionError("Expected tool cancellation")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool.name == "accepted":
                completed.set()

    @input_guardrail
    async def reject(context, agent, input):
        await tool_cleanup_started.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    @input_guardrail
    async def sibling(context, agent, input):
        try:
            await asyncio.Event().wait()
        finally:
            # Cancellation of this guardrail proves that the rejection was published.
            verdict_recorded.set()
            await release_tool_cleanup.wait()
        raise AssertionError("Expected guardrail cancellation")

    agent = Agent(
        name="synthetic",
        tools=[accepted, failed, pending],
        input_guardrails=[reject, sibling],
        model=ScriptedModel(
            [[function_call(name, {}, call_id=name) for name in ("accepted", "failed", "pending")]]
        ),
    )

    async def run():
        config = RunConfig(tracing_disabled=True)
        if streaming:
            result = Runner.run_streamed(agent, "synthetic input", hooks=Hooks(), run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "synthetic input", hooks=Hooks(), run_config=config)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(verdict_recorded.wait(), 2)
        release_tool_cleanup.set()
        with pytest.raises(UserError, match="primary synthetic failure") as caught:
            await asyncio.wait_for(task, 2)
        assert not repeated_cancellation.is_set()
        assert caught.value.run_data is not None
        assert caught.value.run_data.new_items == []
    finally:
        release_tool_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_end_hook_failure_after_rejection_preserves_tool_cleanup(streaming, monkeypatch):
    from agents.run_internal.tool_execution import _FunctionToolBatchExecutor

    started = asyncio.Event()
    hook_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()
    cleanup_finished = asyncio.Event()

    # Hold both cleanups until the hook error has been selected, then run the real drain.
    drain = _FunctionToolBatchExecutor._drain_cancelled_tasks

    async def release_at_drain(self, tasks):
        release.set()
        return await drain(self, tasks)

    monkeypatch.setattr(_FunctionToolBatchExecutor, "_drain_cancelled_tasks", release_at_drain)

    @tool
    async def active() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release.wait()
            cleanup_finished.set()
        return "unexpected"

    @tool
    async def completed() -> str:
        return "done"

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            hook_started.set()
            await cleanup_started.wait()
            raise ValueError("synthetic end hook failure")

    @input_guardrail
    async def reject(context, agent, input):
        await started.wait()
        await hook_started.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    @input_guardrail
    async def sibling(context, agent, input):
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
        raise AssertionError("Expected guardrail cancellation")

    agent = Agent(
        name="synthetic",
        tools=[active, completed],
        input_guardrails=[reject, sibling],
        model=ScriptedModel(
            [[function_call(name, {}, call_id=name) for name in ("active", "completed")]]
        ),
    )
    config = RunConfig(tracing_disabled=True)

    async def run():
        if streaming:
            result = Runner.run_streamed(agent, "synthetic input", hooks=Hooks(), run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "synthetic input", hooks=Hooks(), run_config=config)

    task = asyncio.create_task(run())
    try:
        with pytest.raises(UserError, match="synthetic end hook failure") as caught:
            await asyncio.wait_for(asyncio.shield(task), 2)
        assert cleanup_finished.is_set()
        assert caught.value.run_data is not None
        assert caught.value.run_data.new_items == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_input_rejection_preserves_tool_cleanup_when_runner_cancels_turn():
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    cleaned_up = False

    @tool
    async def active() -> str:
        nonlocal cleaned_up
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            try:
                await release.wait()
                cleaned_up = True
            finally:
                finished.set()
        return "unexpected"

    @input_guardrail
    async def reject(context, agent, input):
        await started.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    agent = Agent(
        name="synthetic",
        tools=[active],
        input_guardrails=[reject],
        model=ScriptedModel([[function_call("active", {}, call_id="active")]]),
    )
    try:
        with pytest.raises(InputGuardrailTripwireTriggered):
            await asyncio.wait_for(
                Runner.run(agent, "synthetic input", run_config=RunConfig(tracing_disabled=True)), 2
            )
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert cleaned_up


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_input_rejection_blocks_invocation_after_tool_validation_suppresses_cancellation(
    streaming: bool,
):
    validating = asyncio.Event()
    validation_cancelled = asyncio.Event()
    sibling_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()
    invoked = False

    @tool_input_guardrail
    async def validate(data):
        validating.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            validation_cancelled.set()
        return ToolGuardrailFunctionOutput.allow()

    @tool(tool_input_guardrails=[validate])
    async def action() -> str:
        nonlocal invoked
        invoked = True
        return "unexpected side effect"

    @input_guardrail
    async def reject(context, agent, input):
        await validating.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    @input_guardrail
    async def sibling(context, agent, input):
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cleanup.set()
            await release_cleanup.wait()
        raise AssertionError("Expected guardrail cancellation")

    agent = Agent(
        name="synthetic",
        tools=[action],
        input_guardrails=[reject, sibling],
        model=ScriptedModel([[function_call("action", {}, call_id="action")]]),
    )

    async def run():
        config = RunConfig(tracing_disabled=True)
        if streaming:
            result = Runner.run_streamed(agent, "synthetic input", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "synthetic input", run_config=config)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(sibling_cleanup.wait(), 2)
        await asyncio.wait_for(validation_cancelled.wait(), 2)
        release_cleanup.set()
        with pytest.raises(InputGuardrailTripwireTriggered):
            await asyncio.wait_for(task, 2)
        assert not invoked
    finally:
        release_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_nested_input_rejection_cancels_pending_outer_guardrail():
    outer_started = asyncio.Event()
    outer_finished = asyncio.Event()

    @input_guardrail
    async def outer_check(context, agent, input) -> GuardrailFunctionOutput:
        outer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            outer_finished.set()
        raise AssertionError("The outer guardrail should be cancelled")

    @input_guardrail(run_in_parallel=False)
    async def nested_reject(context, agent, input):
        await outer_started.wait()
        return GuardrailFunctionOutput(output_info="nested rejection", tripwire_triggered=True)

    nested = Agent(
        name="nested",
        model=ScriptedModel(),
        input_guardrails=[nested_reject],
    ).as_tool(
        tool_name="nested_tool",
        tool_description="Run the nested agent",
        failure_error_function=None,
    )
    outer = Agent(
        name="outer",
        model=ScriptedModel(
            [[function_call("nested_tool", {"input": "nested input"}, call_id="nested")]]
        ),
        input_guardrails=[outer_check],
        tools=[nested],
    )

    with pytest.raises(InputGuardrailTripwireTriggered) as rejection:
        await asyncio.wait_for(
            Runner.run(outer, "outer input", run_config=RunConfig(tracing_disabled=True)),
            timeout=2,
        )

    assert rejection.value.guardrail_result.guardrail is nested_reject
    assert outer_finished.is_set()
