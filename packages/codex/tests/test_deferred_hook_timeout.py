"""A completed restore hook can report its own timeout before the SDK deadline."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from agents import Agent
from tests._asyncio import timeout
from tests.native_fixtures import responses as responses
from tests.test_deferred_jobs import Journal, Provider, declared
from tests.test_deferred_opening_outcome import debug_logs, pending, scope_outcomes
from tests.test_native_manual_tools import provider


class HookTimeout(TimeoutError):
    pass


class AsyncHookTimeout(asyncio.TimeoutError):
    pass


@pytest.mark.parametrize("kind", [HookTimeout, AsyncHookTimeout])
async def test_completed_hook_timeout_preserves_identity(responses, caplog, monkeypatch, kind):
    marker = "private-hook-detail"
    failure = kind(marker)
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)

    class HookedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                raise failure
            async with super().session(**kwargs) as session:
                yield session

    agent = HookedAgent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)
        async with debug_logs(caplog, marker), scope_outcomes(monkeypatch, marker):
            with pytest.raises(kind) as caught:
                async with jobs.start(live, request, timeout_seconds=30):
                    pytest.fail("The failed restore hook entered")
            assert caught.value is failure
    assert journal.log == ["prepare"] and not backend.calls and len(responses.requests) == 1


@pytest.mark.parametrize("hook_exit", ["propagate", "replace", "suppress"])
@pytest.mark.parametrize(
    ("during_entry", "later_cancellations"),
    [(False, 0), (False, 1), (False, 2), (True, 0), (True, 1)],
    ids=["0", "1", "2", "entry", "entry-cancel"],
)
async def test_start_timeout_keeps_hook_outcome_during_cleanup(
    responses, monkeypatch, hook_exit, during_entry, later_cancellations
):
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: clock() + offset)
    cleaning, release, cleaned = (asyncio.Event() for _ in range(3))
    seen, sessions = [], []
    replacement = RuntimeError("Restore cleanup failed")
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)

    class HookedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            nonlocal offset
            async with super().session(**kwargs) as session:
                if kwargs.get("state") is None:
                    yield session
                    return
                sessions.append(session)
                owner = asyncio.current_task()
                offset += 2
                if during_entry:
                    # Finish entering only after the shared deadline cancels this hook.
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.Future()
                try:
                    yield session
                except TimeoutError as first:
                    assert asyncio.current_task() is owner
                    seen.append(first)
                    cleaning.set()
                    await release.wait()
                    if hook_exit == "propagate":
                        raise
                    if hook_exit == "replace":
                        raise replacement from first
                finally:
                    cleaned.set()

    agent = HookedAgent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)

        async def drive():
            try:
                async with jobs.start(live, request, timeout_seconds=1):
                    pytest.fail("Expired start entered the caller body")
            except BaseException as error:
                return error

        driver = asyncio.create_task(drive())
        try:
            await cleaning.wait()
            for index in range(later_cancellations):
                driver.cancel(f"later cancellation {index}")
                await asyncio.sleep(0)
            assert not driver.done() and not cleaned.is_set() and not sessions[0]._closed
        finally:
            release.set()
            (outcome,) = await asyncio.gather(driver, return_exceptions=True)
        assert cleaned.is_set() and sessions[0]._closed
        expected = (
            "Deferred session restore timed out"
            if during_entry
            else "Deferred start timed out before advance"
        )
        assert len(seen) == 1 and str(seen[0]) == expected
        if hook_exit == "propagate":
            assert outcome is seen[0]
        elif hook_exit == "replace":
            assert outcome is replacement and replacement.__cause__ is seen[0]
        else:
            assert isinstance(outcome, RuntimeError)
            assert str(outcome) == "Deferred session closed before entering the start context"
    assert journal.log == ["prepare"] and not backend.calls and len(responses.requests) == 1
