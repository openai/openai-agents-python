"""Actual native completion must not outrun Python handle publication."""

import asyncio

import pytest

from agents import Agent, TurnStoppedError, _native
from tests._asyncio import timeout
from tests.native_fixtures import responses as responses
from tests.test_native_agent import provider
from tests.test_native_agent_observation import text_response


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["none", "cancel", "close"])
async def test_deferred_admission_retains_events_and_joins_before_publication(
    responses, monkeypatch, stop
):
    entered, release = asyncio.Event(), asyncio.Event()
    sampled = asyncio.Event()
    original = _native.NativeThread._submit

    async def held(native, encoded):
        result = await original(native, encoded)
        await sampled.wait()
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # The caller must join an admission whose native receipt is held.
            await release.wait()
            raise
        return result

    async def on_event(event):
        if event["type"] == "response.completed":
            sampled.set()

    owner = provider(responses)
    async with (
        timeout(10),
        Agent(instructions="Answer.", provider=owner).session() as session,
    ):
        responses.respond(*text_response("initial", "first"))
        assert (await (await session.submit("first")).get_result()).final_output == "first"
        responses.before_event = on_event
        monkeypatch.setattr(_native.NativeThread, "_submit", held)
        responses.respond(*text_response("second", *["x"] * 350))
        handle = await session.submit("second")
        result = asyncio.create_task(handle.get_result())
        stopping = None
        try:
            await entered.wait()
            assert not result.done()
            assert handle.turn_id is None
            if stop == "cancel":
                stopping = asyncio.create_task(handle.cancel())
                await asyncio.sleep(0)
                assert not stopping.done()
            elif stop == "close":
                stopping = asyncio.create_task(session._aclose())
                await asyncio.sleep(0)
                assert not stopping.done()
        finally:
            # Always free this test's held native receipt before Session exit,
            # including when an assertion fails. Production cleanup must join.
            release.set()
        if stop == "none":
            assert (await result).final_output == "x" * 350
            assert handle.turn_id is not None
        else:
            assert stopping is not None
            await stopping
            with pytest.raises(TurnStoppedError):
                await result
    await owner.aclose()
    assert len(responses.requests) == 2
