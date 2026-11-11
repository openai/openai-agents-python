"""Guardrail records have been retaining logical-run ownership through steering."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, GuardrailResult, GuardrailTripwire, InputGuardrail, OutputGuardrail
from agents.observation import TurnStarted
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_review_ii_steering_tail import ProjectContinuation


@pytest.mark.parametrize("projected_tail", [False, True])
@pytest.mark.parametrize("tripwire", [False, True])
async def test_steering_continuation_retains_input_and_final_output_records(
    responses, tripwire, projected_tail, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def before_complete(request):
        if request.text == "initial" and not projected_tail:
            entered.set()
            await release.wait()

    def check_output(request, _agent):
        return GuardrailResult(
            output_info=request.final_output,
            tripwire_triggered=tripwire and request.final_output == "after steering",
        )

    responses.respond(assistant_output("initial"))
    responses.respond(assistant_output("after steering"))
    responses.respond(assistant_output("independent"))
    agent = Agent(
        instructions="Answer briefly.",
        provider=provider(responses),
        before_complete=before_complete,
        input_guardrails=[
            InputGuardrail(
                "initial-input",
                lambda _turn, _context, _agent, _input: GuardrailResult(
                    output_info="checked", tripwire_triggered=False
                ),
                run_in_parallel=False,
            )
        ],
        output_guardrails=[OutputGuardrail("final-output", check_output)],
    )
    async with timeout(15), agent.session() as session:
        if projected_tail:
            # Existing Core-controlled fixture: receipt projection only; native
            # IDs, model responses, guard checks and public handles remain real.
            monkeypatch.setattr(session, "_native", ProjectContinuation(session._native))
        turn = await session.submit("begin")
        result_task = asyncio.create_task(turn.get_result())
        if not projected_tail:
            try:
                entered_task = asyncio.create_task(entered.wait())
                done, _ = await asyncio.wait(
                    (result_task, entered_task), return_when=asyncio.FIRST_COMPLETED
                )
                if result_task in done:
                    await result_task
                assert entered.is_set()
                await turn.steer("Continue with the new instruction.")
            finally:
                release.set()
        if tripwire:
            with pytest.raises(GuardrailTripwire) as caught:
                await result_task
            records = caught.value.results
        else:
            completed = await result_task
            assert completed.turn is turn
            assert completed.final_output == "after steering"
            records = completed.guardrail_results
        started = [event async for event in turn.events() if isinstance(event, TurnStarted)]
        assert len(started) == (2 if projected_tail else 1)
        assert len({event.turn_id for event in started}) == len(started)
        assert turn.guardrail_results == records
        assert [record.name for record in records].count("initial-input") == 1
        final = [record for record in records if record.result.output_info == "after steering"]
        assert len(final) == 1 and final[0].kind == "output"
        assert final[0].result.tripwire_triggered is tripwire
        assert all(record.session_id == session.id for record in records)
        retained = turn.guardrail_results
        fresh = await session.submit("New independent root.")
        next_result = await fresh.get_result()
        assert next_result.final_output == "independent"
        assert len(next_result.guardrail_results) == 2
        assert all(record.turn_id == fresh.turn_id for record in next_result.guardrail_results)
        assert turn.guardrail_results == retained
        assert len(responses.requests) == 3
