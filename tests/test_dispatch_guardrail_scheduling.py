import asyncio
from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
)
from agents.testing import ScriptedModel, assistant_message


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("scope", ["agent", "run"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_immediate_parallel_guardrail_prevents_model_entry_and_side_effects(
    streamed: bool, scope: str, asynchronous: bool
) -> None:
    entered: list[str] = []
    effects: list[str] = []

    class RecordingModel(ScriptedModel):
        # This double has been recording provider entry before emitting model output.
        async def get_response(self, *args: Any, **kwargs: Any) -> Any:
            entered.append("response")
            effects.append("provider-side-effect")
            return await super().get_response(*args, **kwargs)

        async def stream_response(self, *args: Any, **kwargs: Any) -> Any:
            entered.append("stream")
            effects.append("provider-side-effect")
            async for event in super().stream_response(*args, **kwargs):
                yield event

    def reject(*args: Any) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    async def reject_async(*args: Any) -> GuardrailFunctionOutput:
        return reject(*args)

    guardrail = InputGuardrail(
        guardrail_function=reject_async if asynchronous else reject, run_in_parallel=True
    )

    model = RecordingModel()
    model.enqueue([assistant_message("done")])
    agent = Agent(
        name="guarded",
        model=model,
        input_guardrails=[guardrail] if scope == "agent" else [],
    )
    config = RunConfig(input_guardrails=[guardrail] if scope == "run" else [])
    with pytest.raises(InputGuardrailTripwireTriggered):
        if streamed:
            result = Runner.run_streamed(agent, "start", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "start", run_config=config)
    assert entered == []
    assert effects == []


async def _complete(agent: Agent[Any], streamed: bool, hooks: Any = None) -> Any:
    if streamed:
        result = Runner.run_streamed(agent, "start", hooks=hooks)
        async for _ in result.stream_events():
            pass
        return result.final_output
    return (await Runner.run(agent, "start", hooks=hooks)).final_output


def _text_agent(name: str, guardrails: Any = ()) -> Agent[Any]:
    model = ScriptedModel()
    model.enqueue([assistant_message("done")])
    return Agent(name=name, model=model, input_guardrails=list(guardrails))


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_nested_runs_restore_their_guarded_callback_scope(streamed: bool) -> None:
    from agents.lifecycle import RunHooks
    from agents.run_internal.guardrails import input_guardrail_dispatch_is_preserved

    observed: list[tuple[str, bool]] = []
    nested_done = asyncio.Event()

    def allow(*args: Any) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    async def outer_allow(*args: Any) -> GuardrailFunctionOutput:
        await nested_done.wait()
        return allow()

    class InnerHooks(RunHooks):
        async def on_agent_start(self, context: Any, agent: Any) -> None:
            observed.append((agent.name, input_guardrail_dispatch_is_preserved()))

    class OuterHooks(RunHooks):
        async def on_agent_start(self, *args: Any) -> None:
            observed.append(("outer-before", input_guardrail_dispatch_is_preserved()))
            assert await _complete(_text_agent("unguarded"), streamed, InnerHooks()) == "done"
            guarded = _text_agent("guarded", [InputGuardrail(guardrail_function=allow)])
            assert await _complete(guarded, streamed, InnerHooks()) == "done"
            observed.append(("outer-after", input_guardrail_dispatch_is_preserved()))
            nested_done.set()

    outer = _text_agent("outer", [InputGuardrail(guardrail_function=outer_allow)])
    assert await _complete(outer, streamed, OuterHooks()) == "done"
    assert observed == [
        ("outer-before", True),
        ("unguarded", False),
        ("guarded", True),
        ("outer-after", True),
    ]
    assert not input_guardrail_dispatch_is_preserved()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_run_spawned_from_guardrail_restores_the_guardrail_scope(streamed: bool) -> None:
    from agents.lifecycle import RunHooks
    from agents.run_internal.guardrails import input_guardrail_dispatch_is_preserved

    observed: list[tuple[str, bool]] = []

    class Hooks(RunHooks):
        async def on_agent_start(self, *args: Any) -> None:
            observed.append(("inner", input_guardrail_dispatch_is_preserved()))

    async def allow(*args: Any) -> GuardrailFunctionOutput:
        observed.append(("guard-before", input_guardrail_dispatch_is_preserved()))
        assert await _complete(_text_agent("inner"), streamed, Hooks()) == "done"
        observed.append(("guard-after", input_guardrail_dispatch_is_preserved()))
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    outer = _text_agent("outer", [InputGuardrail(guardrail_function=allow)])
    assert await _complete(outer, streamed) == "done"
    assert observed == [("guard-before", True), ("inner", False), ("guard-after", True)]
    assert not input_guardrail_dispatch_is_preserved()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_cancelled_guarded_run_does_not_leak_dispatch_scope(streamed: bool) -> None:
    from agents.lifecycle import RunHooks
    from agents.run_internal.guardrails import input_guardrail_dispatch_is_preserved

    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    observed: list[bool] = []

    class WaitingHooks(RunHooks):
        async def on_agent_start(self, *args: Any) -> None:
            observed.append(input_guardrail_dispatch_is_preserved())
            started.set()
            try:
                await release.wait()
            finally:
                observed.append(input_guardrail_dispatch_is_preserved())
                cancelled.set()

    async def pending(*args: Any) -> GuardrailFunctionOutput:
        await release.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    outer = _text_agent("outer", [InputGuardrail(guardrail_function=pending)])
    result = Runner.run_streamed(outer, "start", hooks=WaitingHooks()) if streamed else None

    async def run() -> None:
        if result is not None:
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(outer, "start", hooks=WaitingHooks())

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(started.wait(), 2)
        if result is not None:
            result.cancel()
        else:
            task.cancel()
        settled = await asyncio.gather(task, return_exceptions=True)
        if result is None:
            assert isinstance(settled[0], asyncio.CancelledError)
        else:
            assert result.is_complete
        await asyncio.wait_for(cancelled.wait(), 2)
        assert observed == [True, True]
        assert not input_guardrail_dispatch_is_preserved()

        class NextHooks(RunHooks):
            async def on_agent_start(self, *args: Any) -> None:
                observed.append(input_guardrail_dispatch_is_preserved())

        assert await _complete(_text_agent("next"), streamed, NextHooks()) == "done"
        assert observed == [True, True, False]
    finally:
        release.set()
        if result is not None:
            result.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
