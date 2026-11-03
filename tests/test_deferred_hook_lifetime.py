"""The borrowed Session stays open until the interrupted caller has unwound."""

from __future__ import annotations

import asyncio
import sys
import traceback
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

import pytest

from agents import Agent
from agents.experimental.deferred import JobUpdate
from tests._asyncio import timeout
from tests.native_fixtures import responses as responses
from tests.test_deferred_jobs import Journal, Provider, declared
from tests.test_deferred_opening_outcome import pending
from tests.test_native_manual_tools import provider


@pytest.fixture
def expire(monkeypatch):
    def advance_clock():
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 11)

    return advance_clock


@pytest.mark.parametrize("timeout_type", list(dict.fromkeys((TimeoutError, asyncio.TimeoutError))))
async def test_immediate_hook_timeout_keeps_identity_before_dispatch(responses, timeout_type):
    class HostTimeout(timeout_type):
        pass

    failure = HostTimeout("entry")
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(60), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def denied(**kwargs):
            if kwargs.get("state") is not None:
                raise failure
            async with original(**kwargs) as restored:
                yield restored

        agent.session = denied
        with pytest.raises(timeout_type) as caught:
            async with jobs.start(live, request):
                pytest.fail("The hook refused entry")
        assert caught.value is failure
    assert journal.log == ["prepare"] and not backend.calls


@pytest.mark.parametrize("phase", ["body", "body-task", "provider", "nested-provider"])
async def test_hook_deadline_joins_borrower_before_closing_session(responses, phase, expire):
    active, rollback, release_hook = (asyncio.Event() for _ in range(3))
    events = asyncio.Queue()
    scope = ContextVar("hook_scope", default="caller")
    restored_sessions, host_errors, caller_errors = [], [], []

    async def block():
        active.set()
        try:
            await asyncio.Future()
        finally:
            events.put_nowait("rollback")
            await rollback.wait()
            assert not restored_sessions[0]._closed
            events.put_nowait("rolled back")

    class Backend(Provider):
        async def start(self, claim, arguments):
            if phase.endswith("provider"):
                await block()
                pytest.fail("The interrupted provider returned")
            return await super().start(claim, arguments)

    journal, backend = Journal(), Backend([JobUpdate("pending", receipt="receipt")])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(60), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            owner = asyncio.current_task()
            token = scope.set("owner")
            try:
                async with original(**kwargs) as restored:
                    restored_sessions.append(restored)
                    try:
                        async with timeout(20), timeout(10):
                            yield restored
                    except asyncio.TimeoutError as exc:
                        host_errors.append(exc)
                        raise
                    finally:
                        assert asyncio.current_task() is owner and scope.get() == "owner"
                        events.put_nowait("hook exit")
                        await release_hook.wait()
            finally:
                scope.reset(token)

        agent.session = hook

        async def drive():
            caller = asyncio.current_task()
            try:
                async with jobs.start(live, request, timeout_seconds=45) as (fresh, result):
                    assert phase.startswith("body") and result == "pending"
                    assert fresh is restored_sessions[0]
                    assert asyncio.current_task() is caller and scope.get() == "caller"
                    if phase == "body-task":
                        await asyncio.gather(block())
                    else:
                        await block()
            except BaseException as exc:
                caller_errors.append(exc)

        driver = asyncio.create_task(drive())
        try:
            await active.wait()
            home = Path(restored_sessions[0]._home.name)
            expire()
            assert await events.get() == "rollback"
            assert home.is_dir() and not restored_sessions[0]._closed
            if phase == "nested-provider":
                expire()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            if phase.endswith("provider"):
                driver.cancel("later provider cancellation")
                await asyncio.sleep(0)
                assert not driver.done()
            rollback.set()
            assert await events.get() == "rolled back"
            assert await events.get() == "hook exit"
            driver.cancel("later exit cancellation")
            await asyncio.sleep(0)
            assert not driver.done() and home.is_dir()
            release_hook.set()
            await driver
            assert len(host_errors) == 1 and caller_errors == host_errors
            assert restored_sessions[0]._closed and not home.exists()
            if sys.version_info >= (3, 11):
                # Only the explicit later caller requests remain outstanding.
                assert driver.cancelling() == (2 if phase.endswith("provider") else 1)
        finally:
            rollback.set()
            release_hook.set()
            if not driver.done():
                driver.cancel()
            await driver


@pytest.mark.parametrize("mode", ["regular", "suppress", "replace", "propagate"])
async def test_hook_driven_exit_preserves_the_host_outcome(responses, mode, expire):
    seen, received = [], []
    guard_expired = False
    replacement = PermissionError("host replacement")
    journal, backend = Journal(), Provider([JobUpdate("pending", receipt="receipt")])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(60), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            async with original(**kwargs) as restored:
                try:
                    async with timeout(10):
                        yield restored
                except asyncio.TimeoutError as exc:
                    seen.append(exc)
                    if mode == "replace":
                        raise replacement from exc
                    if mode != "suppress":
                        raise

        agent.session = hook
        try:
            async with jobs.start(live, request, timeout_seconds=45) as (fresh, result):
                assert result == "pending"
                if mode != "regular":
                    expire()
                    # A stalled borrower must wake before this independent guard.
                    try:
                        async with timeout(1):
                            await asyncio.Future()
                    except asyncio.TimeoutError:
                        guard_expired = True
                        raise
        except BaseException as exc:
            received.append(exc)
        assert fresh._closed and not guard_expired
        if mode == "regular":
            assert not seen and not received
        else:
            assert len(seen) == 1
            if mode == "replace":
                assert received == [replacement] and replacement.__cause__ is seen[0]
            elif mode == "propagate":
                assert received == seen
            else:
                assert not received


@pytest.mark.parametrize(
    ("host_expiry", "reason"), [(False, ("first",)), (True, ("first",)), (True, ())]
)
async def test_first_caller_cancel_survives_provider_rollback_and_hook_exit(
    responses, expire, host_expiry, reason
):
    entered, retiring, release, exiting, close = (asyncio.Event() for _ in range(5))
    caller_errors, hook_errors = [], []

    class Backend(Provider):
        async def start(self, claim, arguments):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                retiring.set()
                await release.wait()

    jobs, tool = declared(Journal(), Backend([]))
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(60), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            async with original(**kwargs) as restored:
                try:
                    async with timeout(10):
                        yield restored
                except BaseException as exc:
                    hook_errors.append(exc)
                    raise
                finally:
                    exiting.set()
                    await close.wait()

        agent.session = hook

        async def drive():
            try:
                async with jobs.start(live, request, timeout_seconds=45):
                    pytest.fail("Cancelled advancement entered the caller body")
            except BaseException as exc:
                caller_errors.append(exc)

        driver = asyncio.create_task(drive())
        try:
            await entered.wait()
            driver.cancel(*reason)
            await retiring.wait()
            if host_expiry:
                expire()
            driver.cancel("later during provider rollback")
            await asyncio.sleep(0)
            assert not driver.done() and not exiting.is_set()
            release.set()
            await exiting.wait()
            driver.cancel("later during hook exit")
            await asyncio.sleep(0)
            assert not driver.done()
            close.set()
            await driver
            assert len(caller_errors) == len(hook_errors) == 1
            assert isinstance(caller_errors[0], asyncio.CancelledError)
            assert caller_errors[0].args == reason
            if host_expiry:
                assert caller_errors[0].__context__ is hook_errors[0]
            else:
                assert caller_errors[0] is hook_errors[0]
        finally:
            release.set()
            close.set()
            if not driver.done():
                driver.cancel()
            await driver


@pytest.mark.parametrize("mode", ["propagate", "replace", "suppress"])
async def test_interruption_during_hook_handoff_never_publishes_a_closed_session(responses, mode):
    seen, sessions = [], []
    replacement = LookupError("hook exit")
    backend = Provider([])
    jobs, tool = declared(Journal(), backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(10), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            async with original(**kwargs) as restored:
                sessions.append(restored)
                try:
                    async with timeout(0):
                        yield restored
                except asyncio.TimeoutError as exc:
                    seen.append(exc)
                    if mode == "replace":
                        raise replacement from exc
                    if mode != "suppress":
                        raise

        agent.session = hook
        with pytest.raises(Exception) as caught:
            async with jobs.start(live, request):
                pytest.fail("The hook closed before the handoff")
        assert sessions[0]._closed and not backend.calls
        assert len(seen) == 1
        if mode == "propagate":
            assert caught.value is seen[0]
        elif mode == "replace":
            assert caught.value is replacement and replacement.__cause__ is seen[0]
        else:
            assert str(caught.value) == "Deferred session closed before entering the start context"


async def test_suppressed_interruption_during_advance_cannot_publish_the_session(responses, expire):
    order = []

    class Backend(Provider):
        async def start(self, claim, arguments):
            expire()
            try:
                await asyncio.Future()
            finally:
                order.append("provider retired")

    jobs, tool = declared(Journal(), Backend([]))
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(60), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            async with original(**kwargs) as restored:
                try:
                    async with timeout(10):
                        yield restored
                except asyncio.TimeoutError:
                    order.append("hook suppressed")

        agent.session = hook
        with pytest.raises(RuntimeError, match="closed before entering the start context"):
            async with jobs.start(live, request, timeout_seconds=45):
                pytest.fail("Suppressed interruption cannot yield a closed Session")
        assert order == ["provider retired", "hook suppressed"]


async def test_hook_interruption_before_handoff_keeps_its_outcome_during_exit(responses):
    retiring, release = asyncio.Event(), asyncio.Event()
    failure = LookupError("host exit")
    backend = Provider([])
    jobs, tool = declared(Journal(), backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(10), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            async with original(**kwargs) as restored:
                try:
                    async with timeout(0):
                        yield restored
                except asyncio.TimeoutError as exc:
                    retiring.set()
                    await release.wait()
                    raise failure from exc

        agent.session = hook

        async def use():
            async with jobs.start(live, request):
                pytest.fail("Interrupted entry published a Session")

        driver = asyncio.create_task(use())
        try:
            await retiring.wait()
            driver.cancel("later")
            await asyncio.sleep(0)
            assert not driver.done()
        finally:
            release.set()
        with pytest.raises(LookupError) as caught:
            await driver
        assert caught.value is failure and not backend.calls


@pytest.mark.parametrize("active_cancel", [True, False], ids=["unwinding", "handled-before-start"])
async def test_hook_wake_preserves_only_a_cancellation_unwinding_the_body(
    responses, expire, active_cancel
):
    entered, unwinding, release = (asyncio.Event() for _ in range(3))
    first, received = [], []
    replacement = LookupError("hook exit")
    backend = Provider([JobUpdate("pending", receipt="receipt")])
    jobs, tool = declared(Journal(), backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(60), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            async with original(**kwargs) as restored:
                try:
                    async with timeout(10):
                        yield restored
                except asyncio.TimeoutError as exc:
                    raise replacement from exc

        agent.session = hook

        async def borrow():
            async with jobs.start(live, request, timeout_seconds=45):
                entered.set()
                if active_cancel:
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError as exc:
                        first.append(exc)
                        unwinding.set()
                        await release.wait()
                        raise
                else:
                    unwinding.set()
                    await release.wait()

        async def drive():
            try:
                if active_cancel:
                    await borrow()
                else:
                    try:
                        raise asyncio.CancelledError("already handled")
                    except asyncio.CancelledError:
                        await borrow()
            except BaseException as exc:
                received.append(exc)

        driver = asyncio.create_task(drive())
        try:
            await entered.wait()
            if active_cancel:
                driver.cancel("first")
            await unwinding.wait()
            due = asyncio.Event()
            asyncio.get_running_loop().call_later(10, due.set)
            expire()
            await due.wait()
            await asyncio.sleep(0)
        finally:
            release.set()
            await driver
        if active_cancel:
            assert received == first and received[0].args == ("first",)
            assert "LookupError: hook exit" in "".join(traceback.format_exception(received[0]))
        else:
            assert received == [replacement]


@pytest.mark.parametrize("hook_exit", ["propagate", "replace", "suppress", "sdk-only"])
async def test_hook_retiring_during_advance_cleanup_controls_body(responses, expire, hook_exit):
    cleaning, release = asyncio.Event(), asyncio.Event()
    order, sessions, seen = [], [], []
    replacement = LookupError("hook exit")

    class Backend(Provider):
        async def start(self, claim, arguments):
            expire()  # Only the SDK's 10-second deadline is due.
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                order.append("provider cleaned")

    jobs, tool = declared(Journal(), Backend([]))
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(60), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            async with original(**kwargs) as restored:
                sessions.append(restored)
                try:
                    async with timeout(20):
                        yield restored
                except asyncio.TimeoutError as exc:
                    seen.append(exc)
                    if hook_exit == "replace":
                        raise replacement from exc
                    if hook_exit != "suppress":
                        raise
                finally:
                    order.append("hook exit")

        agent.session = hook

        async def drive():
            async with jobs.start(live, request, timeout_seconds=10) as (fresh, outcome):
                order.append("body")
                assert fresh is sessions[0] and not fresh._closed
                return outcome

        driver = asyncio.create_task(drive())
        try:
            await cleaning.wait()
            if hook_exit != "sdk-only":
                # The hook deadline is already scheduled before this gate.
                due = asyncio.Event()
                asyncio.get_running_loop().call_later(10, due.set)
                expire()
                await due.wait()
            assert not order and not driver.done() and not sessions[0]._closed
        finally:
            release.set()
            (outcome,) = await asyncio.gather(driver, return_exceptions=True)

        with pytest.raises(RuntimeError, match="^Session is closed$"):
            await sessions[0].submit("Continue.")
        if hook_exit == "sdk-only":
            assert order == ["provider cleaned", "body", "hook exit"]
            assert outcome == "unknown" and not seen
        else:
            assert order == ["provider cleaned", "hook exit"]
            assert len(seen) == 1
            if hook_exit == "propagate":
                assert outcome is seen[0]
            elif hook_exit == "replace":
                assert outcome is replacement and replacement.__cause__ is seen[0]
            else:
                assert isinstance(outcome, RuntimeError)
                assert str(outcome) == "Deferred session closed before entering the start context"
