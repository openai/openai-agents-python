"""Public profile activation/restore retains actual model wire controls and clocks."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, NativeError
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_model_invocation_deadline import (
    Gate,
    deadline_peer as deadline_peer,
    provider_for as deadline_provider,
)
from tests.test_native_request_controls import provider_for

pytestmark = pytest.mark.asyncio
MODEL = "gpt-5.5"


def graph(provider, *, deadline=False):
    target = Agent(
        name="B",
        instructions="B answers.",
        model=MODEL,
        model_settings=(
            {"invocation_timeout_ms": 50}
            if deadline
            else {
                "max_output_tokens": 23,
                "temperature": 0.4,
                "metadata": {"profile": "B"},
            }
        ),
    )
    source = Agent(
        name="A",
        instructions="Route to B.",
        model=MODEL,
        handoffs=[target],
        provider=provider,
        model_settings=(
            {"invocation_timeout_ms": 5000}
            if deadline
            else {
                "max_output_tokens": 11,
                "temperature": 0.2,
                "metadata": {"profile": "A"},
            }
        ),
    )
    return source, target


async def test_profile_wire_controls_survive_handoff_restore_and_sparse_update(responses):
    responses.respond(function_call("transfer_to_b", {}, "handoff-b"))
    for _ in range(4):
        responses.respond(assistant_output("accepted"))
    async with timeout(25), provider_for(responses) as provider:
        source, target = graph(provider)
        async with source.session() as session:
            assert (
                await (await session.submit("Route to B")).get_result()
            ).final_output == "accepted"
            assert await session.get_active_agent() is target
            saved = await session.save_state()
        async with source.session(state=saved) as restored:
            await (await restored.submit("Use saved B")).get_result()
            await restored.update(model_settings={"max_output_tokens": 47, "temperature": None})
            await (await restored.submit("Use patched B")).get_result()
            patched = await restored.save_state()
        async with source.session(state=patched) as restored:
            await (await restored.submit("Retain patched B")).get_result()
    assert len(responses.requests) == 5
    for index, request in enumerate(responses.requests):
        assert request["max_output_tokens"] == (11 if index == 0 else 23 if index < 3 else 47)
        assert request["metadata"] == {"profile": "A" if index == 0 else "B"}
        if index < 3:
            assert request["temperature"] == (0.2 if index == 0 else 0.4)
        else:
            assert "temperature" not in request


@pytest.mark.parametrize("restore", [False, True], ids=["idle-B", "restored-B"])
async def test_profile_model_deadline_uses_destination_clock(deadline_peer, restore):
    gate = Gate()
    deadline_peer.terminal_gate = gate
    deadline_peer.allow_disconnect = True
    deadline_peer.respond(assistant_output("must not complete"))
    async with timeout(20), deadline_provider(deadline_peer) as provider:
        source, target = graph(provider, deadline=True)
        saved = None
        if restore:
            async with source.session() as session:
                await session.update(agent=target)
                saved = await session.save_state()
        try:
            async with source.session(state=saved) as session:
                if not restore:
                    await session.update(agent=target)
                turn = await session.submit("B owns the model clock")
                with pytest.raises(NativeError, match="local model invocation deadline exceeded"):
                    await asyncio.wait_for(turn.get_result(), timeout=3)
                assert gate.entered.is_set()
        finally:
            gate.release.set()
    assert len(deadline_peer.requests) == 1


async def test_managed_profile_lite_rejection_preserves_accepted_model_and_controls(responses):
    responses.respond(assistant_output("still standard"))
    async with timeout(30), provider_for(responses) as provider:
        async with Agent(
            name="A",
            model=MODEL,
            instructions="Answer.",
            provider=provider,
            model_settings={"max_output_tokens": 31},
            handoffs=[Agent(name="B", instructions="B answers.")],
        ).session() as session:
            with pytest.raises(
                (ValueError, RuntimeError), match="standard Responses|Responses Lite"
            ):
                await session.update(model="gpt-5.6-sol")
            result = await (await session.submit("old settings remain")).get_result()
    assert result.final_output == "still standard"
    assert len(responses.requests) == 1 and responses.requests[0]["model"] == MODEL
    assert responses.requests[0]["max_output_tokens"] == 31
