"""Admission must close native work if its acknowledgement is lost."""

from __future__ import annotations

import json

import pytest

from agents import Agent
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider


def agent_for(peer: ResponsesFixture, **kwargs):
    return Agent(
        instructions="Use the supplied tools when asked.",
        model="gpt-5.5",
        provider=provider(peer),
        **kwargs,
    )


@pytest.mark.parametrize("after_native_acceptance", [False, True])
async def test_lost_ack_closes_actual_owner_and_late_submission_cannot_reopen(
    responses, after_native_acceptance
):
    agent = agent_for(responses)
    native = None
    failure = OSError("synthetic lost native acknowledgement")
    request = None
    async with timeout(20), agent.session() as session:
        native = session._native

        class LostAck:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _submit(self, value):
                nonlocal request
                request = value
                if after_native_acceptance:
                    receipt = json.loads(await native._submit(value))
                    assert receipt["status"] == "started"
                raise failure

        session._native = LostAck()
        if after_native_acceptance:
            responses.respond(assistant_output("possibly sent before acknowledgement loss"))
        submitted = await session.submit("unconfirmed input")
        with pytest.raises(OSError) as caught:
            await submitted.get_result()
        assert caught.value is failure
        before = len(responses.requests)
        assert before <= int(after_native_acceptance)
        with pytest.raises(RuntimeError, match="cannot continue"):
            await session.submit("late public input")
        with pytest.raises(Exception, match="closed|stopped|shutdown"):
            await native._submit(request)
        assert len(responses.requests) == before
