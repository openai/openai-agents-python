import asyncio

import pytest

from agents.agent import Agent
from agents.guardrail import GuardrailFunctionOutput, output_guardrail
from agents.run_context import RunContextWrapper
from agents.run_internal.guardrails import run_output_guardrails


@pytest.mark.asyncio
async def test_output_guardrail_sequential_execution():
    execution_order = []

    @output_guardrail(run_in_parallel=False, name="seq1")
    async def seq_guardrail_1(ctx, agent, output):
        await asyncio.sleep(0.05)
        execution_order.append("seq1")
        return GuardrailFunctionOutput(tripwire_triggered=False, output_info=None)

    @output_guardrail(run_in_parallel=False, name="seq2")
    async def seq_guardrail_2(ctx, agent, output):
        execution_order.append("seq2")
        return GuardrailFunctionOutput(tripwire_triggered=False, output_info=None)

    @output_guardrail(run_in_parallel=True, name="par1")
    async def par_guardrail_1(ctx, agent, output):
        await asyncio.sleep(0.02)
        execution_order.append("par1")
        return GuardrailFunctionOutput(tripwire_triggered=False, output_info=None)

    @output_guardrail(run_in_parallel=True, name="par2")
    async def par_guardrail_2(ctx, agent, output):
        execution_order.append("par2")
        return GuardrailFunctionOutput(tripwire_triggered=False, output_info=None)

    agent = Agent(name="test")
    ctx = RunContextWrapper(context=None)
    guardrails = [par_guardrail_1, seq_guardrail_1, par_guardrail_2, seq_guardrail_2]

    results = await run_output_guardrails(
        guardrails=guardrails, agent=agent, agent_output="test_output", context=ctx, results_sink=[]
    )

    assert len(results) == 4
    # Sequential ones should run first and in order, then parallel ones run concurrently
    # par2 will finish before par1 because par1 sleeps
    assert execution_order == ["seq1", "seq2", "par2", "par1"]
