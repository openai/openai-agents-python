from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrail,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
)
from agents.run_internal.guardrails import run_output_guardrails


@pytest.mark.asyncio
async def test_run_output_guardrails_awaits_cancelled_tasks():
    """When one output guardrail trips, sibling guardrails must be awaited after cancel.

    Regression test: ``run_output_guardrails`` previously cancelled sibling tasks but
    did not await them, which can leak pending tasks and emit
    ``Task was destroyed but it is pending!`` warnings. The input-guardrail variants
    already await on cancel; the output variant should match.
    """

    slow_started = asyncio.Event()
    cancelled_observed = asyncio.Event()

    async def slow_then_observe_cancel(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        slow_started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled_observed.set()
            raise
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    async def fast_tripwire(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        # Wait until the slow guardrail is actually parked on its sleep so the
        # subsequent cancel hits the installed CancelledError handler. Without
        # this, the slow task could be cancelled before reaching the try block
        # and ``cancelled_observed`` would stay unset even with the production
        # fix in place.
        await slow_started.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    guardrails = [
        OutputGuardrail(guardrail_function=slow_then_observe_cancel),
        OutputGuardrail(guardrail_function=fast_tripwire),
    ]
    agent: Agent[Any] = Agent(name="test")
    context: RunContextWrapper[Any] = RunContextWrapper(context=None)

    with pytest.raises(OutputGuardrailTripwireTriggered):
        await run_output_guardrails(guardrails, agent, "agent output", context)

    # The slow guardrail must have observed cancellation and finished before
    # ``run_output_guardrails`` returned. If it had not been awaited, this event
    # would still be unset at this point.
    assert cancelled_observed.is_set()
