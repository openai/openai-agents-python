"""Replacement output checks belong to the original native Turn."""

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    GuardrailResult,
    GuardrailTripwire,
    OutputGuardrail,
    PromptedOutput,
    RecoverRun,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent_observation import provider


class Record(BaseModel):
    count: int


@pytest.mark.parametrize("tripwire", [False, True])
async def test_recovery_guardrail_records_remain_on_the_owning_turn(responses, tripwire):
    responses.respond(assistant_output("invalid output"))
    native = provider(responses)

    async def check(context, actual_agent):
        assert context.final_output == Record(count=3)
        return GuardrailResult(tripwire, output_info="replacement verdict")

    agent = Agent(
        name="recovery owner",
        model="gpt-5.5",
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
        output_guardrails=[OutputGuardrail("replacement", check)],
    )
    try:
        async with timeout(20), agent.session() as session:
            turn = await session.submit("Return a record.")

            def recover(_):
                return RecoverRun(Record(count=3), include_in_history=False)

            if tripwire:
                with pytest.raises(GuardrailTripwire) as caught:
                    await session._one_shot_result(turn, recover, True, "Return a record.")
                assert caught.value.record.name == "replacement"
            else:
                result = await session._one_shot_result(turn, recover, True, "Return a record.")
                assert result.final_output == Record(count=3)
                assert result.guardrail_results == turn.guardrail_results
            assert len(turn.guardrail_results) == 1
            record = turn.guardrail_results[0]
            assert record.name == "replacement" and record.kind == "output"
            assert record.agent is agent
            assert record.session_id == session.id and record.turn_id == turn.turn_id
            assert record.result.tripwire_triggered is tripwire
            assert record.result.output_info == "replacement verdict"
            assert len(responses.requests) == 1
    finally:
        await native.aclose()
