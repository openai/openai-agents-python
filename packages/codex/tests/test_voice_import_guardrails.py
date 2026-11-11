"""Classic Realtime rejects controls belonging to the native engine before admission."""

import asyncio
from typing import Any, cast

import pytest

from agents.realtime import (
    RealtimeAgent,
    RealtimeOutputGuardrail,
    RealtimeRunConfig,
    RealtimeRunner,
)
from agents.realtime.model_events import RealtimeModelToolCallEvent
from agents.realtime.model_inputs import RealtimeModelSendInterrupt, RealtimeModelSendSessionUpdate
from agents.realtime.testing import ScriptedRealtimeModel
from tests._asyncio import timeout


def native_guard() -> Any:
    def must_not_run(_input):
        pytest.fail("Classic must not invoke a native guardrail callback")

    return RealtimeOutputGuardrail(name="native-only", callback=must_not_run)


@pytest.mark.parametrize("source", ["agent", "config"])
async def test_native_guardrail_rejected_before_classic_connect(source):
    agent = RealtimeAgent(name="Legacy")
    config: RealtimeRunConfig = {"tracing_disabled": True}
    if source == "agent":
        agent.output_guardrails.append(native_guard())
    else:
        config["output_guardrails"] = [native_guard()]
    model = ScriptedRealtimeModel(strict=False)
    session = await RealtimeRunner(agent, model=model, config=config).run()
    with pytest.raises(TypeError, match=r"Classic.*run\(\)"):
        async with session:
            pytest.fail("An incompatible guardrail must prevent connection")
    assert model.connect_calls == ()
    assert model.sent_events == ()


async def test_native_guardrail_rejected_before_classic_agent_update():
    model = ScriptedRealtimeModel(strict=False)
    session = await RealtimeRunner(
        RealtimeAgent(name="Original"), model=model, config={"tracing_disabled": True}
    ).run()
    async with session:
        with pytest.raises(TypeError, match=r"Classic.*run\(\)"):
            await session.update_agent(
                RealtimeAgent(name="Invalid", output_guardrails=[native_guard()])
            )
        assert model.sent_events == ()
        await session.send_audio(b"\x01\x00")
    assert model.closed


async def test_native_guardrail_rejected_before_classic_handoff_activation():
    invalid = RealtimeAgent(name="Invalid", output_guardrails=[native_guard()])
    model = ScriptedRealtimeModel(strict=False)
    session = await RealtimeRunner(
        RealtimeAgent(name="Original", handoffs=[invalid]),
        model=model,
        config={"tracing_disabled": True},
    ).run()
    async with session:
        await model.emit(
            RealtimeModelToolCallEvent(
                name="transfer_to_invalid", call_id="handoff", arguments="{}"
            )
        )
        events = []
        async with timeout(2):
            async for event in session:
                events.append(event)
                if event.type in {"error", "handoff"}:
                    break
        assert events[-1].type == "error"
        assert not any(
            isinstance(event, RealtimeModelSendSessionUpdate) for event in model.sent_events
        )


async def test_duck_typed_classic_guardrail_still_checks_streamed_output():
    from agents.classic import GuardrailFunctionOutput, OutputGuardrail
    from agents.realtime.model_events import (
        RealtimeModelOutputTextDeltaEvent,
        RealtimeModelTurnStartedEvent,
    )

    checked = asyncio.Event()

    async def check(_context, _agent, text):
        assert text == "blocked"
        checked.set()
        return GuardrailFunctionOutput(output_info="blocked", tripwire_triggered=True)

    wrapped = OutputGuardrail(guardrail_function=check)

    class DuckGuard:
        async def run(self, *args):
            return await wrapped.run(*args)

    model = ScriptedRealtimeModel(strict=False)
    session = await RealtimeRunner(
        RealtimeAgent(name="Legacy", output_guardrails=[cast(Any, DuckGuard())]),
        model=model,
        config={"tracing_disabled": True, "guardrails_settings": {"debounce_text_length": 1}},
    ).run()
    async with session:
        await model.emit(
            RealtimeModelTurnStartedEvent(response_id="response"),
            RealtimeModelOutputTextDeltaEvent(
                item_id="item", delta="blocked", response_id="response"
            ),
        )
        async with timeout(2):
            await checked.wait()
            async for event in session:
                if event.type == "guardrail_tripped":
                    break
        assert any(isinstance(event, RealtimeModelSendInterrupt) for event in model.sent_events)
