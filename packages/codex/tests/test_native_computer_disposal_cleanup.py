"""Provider lease retirement after a Computer disposal failure."""

import asyncio

import pytest

from agents import Agent, ComputerProvider, NativeProvider, computer_tool, get_cleanup
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_computer import computer_call
from tests.test_native_computer_adapter import Driver


@pytest.mark.parametrize("mode", ["error", "cancelled", "lease_failure"])
async def test_disposal_failure_still_releases_native_provider(responses, mode):
    problem = (
        asyncio.CancelledError("synthetic Computer cancellation")
        if mode == "cancelled"
        else RuntimeError("synthetic Computer disposal failure")
    )
    secondary = RuntimeError("synthetic lease release failure")
    entered, release = asyncio.Event(), asyncio.Event()
    driver = Driver()
    disposed = []

    async def dispose(value):
        disposed.append(value)
        entered.set()
        await release.wait()
        raise problem

    responses.respond(computer_call(actions=[{"type": "screenshot"}]))
    responses.respond(assistant_output("done"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    agent = Agent(
        instructions="Use computer.",
        model="gpt-5",
        provider=provider,
        tools=[computer_tool(ComputerProvider(lambda _: driver, dispose))],
    )
    scope = agent.session()
    session = await scope.__aenter__()
    try:
        turn = await session.submit("Screenshot.")
        assert (await turn.get_result()).final_output == "done"
        attempts = []
        if mode == "lease_failure":
            original = session._provider_lease

            class Lease:
                def release(self):
                    attempts.append("release")
                    if len(attempts) == 1:
                        raise secondary
                    original.release()

            session._provider_lease = Lease()
        closing = asyncio.create_task(session.aclose())
        await entered.wait()
        assert not closing.done()
        with pytest.raises(RuntimeError, match="open or opening"):
            await provider.aclose()
        release.set()
        with pytest.raises(BaseException) as caught:
            await closing
        if mode == "cancelled":
            assert isinstance(caught.value, asyncio.CancelledError)
        else:
            assert caught.value is problem
        if mode == "lease_failure":
            assert attempts == ["release"]
            assert get_cleanup(problem) is session
            chain, current = [], problem.__context__
            while current is not None:
                chain.append(current)
                current = current.__context__
            assert secondary in chain
            await session.aclose()
            assert attempts == ["release", "release"]
        await provider.aclose()
        assert disposed == [driver]
    finally:
        release.set()
        await session.aclose()
        await scope.__aexit__(None, None, None)
        await provider.aclose()
