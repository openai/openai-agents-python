"""A rejected public entry retains cleanup ownership until an explicit retry joins it."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from agents import Agent, NativeProvider
from agents.realtime import NativeRealtimeConnection, RealtimeAdapterContext, RealtimeConfig
from tests._asyncio import timeout
from tests.native_fixtures import text_response
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


async def test_new_entry_retries_unpublished_startup_cleanup(realtime_peer: RealtimePeer) -> None:
    peer = realtime_peer
    startup_error = RuntimeError("Synthetic startup send failure")
    cleanup_error = LookupError("Synthetic startup cleanup failure")
    retry_entered = asyncio.Event()
    release_retry = asyncio.Event()
    allow_close = False
    closed = False
    frames: asyncio.Queue[str | None] = asyncio.Queue()

    class Connection:
        @property
        def incoming(self) -> AsyncIterator[str]:
            return self.receive()

        async def receive(self) -> AsyncIterator[str]:
            while (frame := await frames.get()) is not None:
                yield frame

        async def send(self, frame: str) -> None:
            raise startup_error

        async def aclose(self) -> None:
            nonlocal closed
            if not allow_close:
                raise cleanup_error
            retry_entered.set()
            await release_retry.wait()
            frames.put_nowait(None)
            closed = True

    class Adapter:
        async def connect(self, context: RealtimeAdapterContext) -> NativeRealtimeConnection:
            return Connection()

    peer.responses.respond(*text_response("after-startup-retry", "Caller is still open."))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="fixture", supports_websockets=False
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        try:
            # Deliberately retain no failed owner: inline async-with is public usage.
            with pytest.raises(RuntimeError, match="Synthetic startup send failure") as failure:
                async with session.realtime(
                    config=RealtimeConfig(model="fixture-realtime", adapter=Adapter())
                ):
                    pytest.fail("Failing adapter started")
            assert failure.value is startup_error
            # Native transport failure may remain between the original error
            # and its cleanup context; preserve both existing error chains.
            pending: list[BaseException] = [failure.value]
            errors: set[int] = set()
            while pending:
                error = pending.pop()
                if id(error) in errors:
                    continue
                errors.add(id(error))
                pending.extend(
                    item for item in (error.__cause__, error.__context__) if item is not None
                )
            assert id(cleanup_error) in errors

            with pytest.raises(LookupError) as retry_failure:
                async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                    pytest.fail("Failed cleanup released the previous owner")
            assert retry_failure.value is cleanup_error
            assert not closed
            assert peer.connections == 0

            allow_close = True
            replacement = session.realtime(config=RealtimeConfig(model="fixture-realtime"))
            opening = asyncio.create_task(replacement.__aenter__())
            try:
                await retry_entered.wait()
                assert not opening.done()
                assert not closed
                assert peer.connections == 0
                release_retry.set()
                assert await opening is replacement
                assert closed
                assert peer.connections == 1
            finally:
                release_retry.set()
                await replacement.aclose()
                await asyncio.gather(opening, return_exceptions=True)

            turn = await session.submit("Check caller ownership.")
            assert (await turn.get_result()).final_output == "Caller is still open."
        finally:
            allow_close = True
            release_retry.set()
