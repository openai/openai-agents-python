"""A joined interruption releases the Session's admission gate."""

from __future__ import annotations

import asyncio

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses


async def test_graceful_graph_join_allows_immediate_submit(
    responses: ResponsesFixture, monkeypatch
):
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    agent = Agent(
        instructions="Answer.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-graph-join"),
    )
    async with timeout(20), agent.session() as session:
        first = await session.submit("first")
        assert (await first.get_result()).final_output == "first"
        interrupt_all = session._interrupt_all

        async def stop(mode, state, accepted):
            native_acceptance = asyncio.get_running_loop().create_future()
            await interrupt_all(mode, state, native_acceptance)
            native_acceptance.result()
            # Wake the joiner just before the owned task finishes. The joiner
            # can then see a done task before its done callback runs.
            accepted.set_result(None)
            await asyncio.sleep(0)

        monkeypatch.setattr(session, "_interrupt_all", stop)
        await session.cancel_all(mode="graceful")
        second = await session.submit("second")
        assert second is not first
        assert (await second.get_result()).final_output == "second"
        assert len(responses.requests) == 2
