"""Observe terminal submissions through the same Runner interfaces an app uses."""

import asyncio
from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
)
from agents.decorators import output_guardrail, tool
from agents.plugins import Plugin, PluginHooks, RunOutcome
from agents.testing import ScriptedModel, assistant_message, function_call

CFG = RunConfig(tracing_disabled=True)


def observer(seen: list[RunOutcome]) -> Plugin[Any]:
    return Plugin(name="outcomes", hooks=PluginHooks(on_run_end=seen.append))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_submission_waits_for_guardrails_and_keeps_failures_private(stream: bool) -> None:
    seen: list[RunOutcome] = []

    @output_guardrail
    async def block(ctx: Any, agent: Any, output: Any) -> GuardrailFunctionOutput:
        assert not seen
        return GuardrailFunctionOutput(output_info="secret diagnostic", tripwire_triggered=True)

    agent = Agent(
        "private agent",
        model=ScriptedModel([[assistant_message("private answer")]]),
        plugins=[observer(seen)],
        output_guardrails=[block],
    )
    with pytest.raises(OutputGuardrailTripwireTriggered):
        if stream:
            result = Runner.run_streamed(agent, "private request", run_config=CFG)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "private request", run_config=CFG)
    assert [s.status for s in seen] == ["failed"]
    assert "private" not in repr(seen) and "secret" not in repr(seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_approval_pause_and_resume_are_distinct_submissions(stream: bool) -> None:
    seen: list[RunOutcome] = []
    effects: list[str] = []

    @tool(needs_approval=True)
    def save() -> str:
        effects.append("saved")
        return "ok"

    agent = Agent(
        "test",
        tools=[save],
        plugins=[observer(seen)],
        model=ScriptedModel(
            [[function_call("save", {}, call_id="c1")], [assistant_message("done")]]
        ),
    )

    async def submit(value: Any) -> Any:
        if not stream:
            return await Runner.run(agent, value, run_config=CFG)
        res = Runner.run_streamed(agent, value, run_config=CFG)
        async for _ in res.stream_events():
            pass
        return res

    first = await submit("save")
    assert [s.status for s in seen] == ["paused"] and effects == []
    state = first.to_state()
    state.approve(first.interruptions[0])
    final = await submit(state)
    assert final.final_output == "done" and effects == ["saved"]
    assert [s.status for s in seen] == ["paused", "completed"]
    assert seen[0] is not seen[1]
    assert all(s.elapsed_seconds >= 0 for s in seen)


@pytest.mark.asyncio
async def test_observer_failure_and_repeated_drain_do_not_change_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[RunOutcome] = []

    async def broken(_: RunOutcome) -> None:
        raise ValueError("private export token")

    agent = Agent(
        "test",
        model=ScriptedModel([[assistant_message("ok")]]),
        plugins=[Plugin(name="broken", hooks=PluginHooks(on_run_end=broken)), observer(seen)],
    )
    run = Runner.run_streamed(agent, "hello", run_config=CFG)
    async for _ in run.stream_events():
        pass
    async for _ in run.stream_events():
        pass
    assert run.final_output == "ok"
    assert [s.status for s in seen] == ["completed"]
    assert "private export token" not in caplog.text
    assert "plugin_observer_failed on_run_end" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cancellation_of_running_tool_reports_once(stream: bool) -> None:
    seen: list[RunOutcome] = []
    began = asyncio.Event()
    block = asyncio.Event()

    @tool
    async def wait_for_work() -> str:
        began.set()
        await block.wait()
        return "finished"

    agent = Agent(
        "test",
        tools=[wait_for_work],
        plugins=[observer(seen)],
        model=ScriptedModel([[function_call("wait_for_work", {}, call_id="wait")]]),
    )
    if stream:
        result = Runner.run_streamed(agent, "start", run_config=CFG)
        await asyncio.wait_for(began.wait(), 5)
        result.cancel()
        async for _ in result.stream_events():
            pass
    else:
        running = asyncio.create_task(Runner.run(agent, "start", run_config=CFG))
        await asyncio.wait_for(began.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert [s.status for s in seen] == ["cancelled"]


@pytest.mark.asyncio
async def test_parallel_submissions_with_shared_plugin_do_not_mix_receipts() -> None:
    seen: list[RunOutcome] = []
    plugin = observer(seen)
    agents = [
        Agent("test", model=ScriptedModel([[assistant_message(value)]]), plugins=[plugin])
        for value in ("a", "b")
    ]
    results = await asyncio.gather(*(Runner.run(a, "hello", run_config=CFG) for a in agents))
    assert [r.final_output for r in results] == ["a", "b"]
    assert [s.status for s in seen] == ["completed", "completed"]
    assert seen[0] is not seen[1]


@pytest.mark.asyncio
async def test_cancelled_input_filter_never_reports_stream_success() -> None:
    seen: list[RunOutcome] = []

    async def stop(_: Any) -> Any:
        raise asyncio.CancelledError()

    model = ScriptedModel()
    agent = Agent(
        "test",
        model=model,
        plugins=[observer(seen), Plugin(name="stop", hooks=PluginHooks(filter_input=stop))],
    )
    result = Runner.run_streamed(agent, "private", run_config=CFG)
    async for _ in result.stream_events():
        pass
    assert not model.calls
    assert [event.status for event in seen] == ["cancelled"]
