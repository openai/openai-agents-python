"""Source-only checks of the task-affine Environment owner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import pytest

from _agents_codex._environment_lifetime import _EnvironmentLease, _release_environment
from _agents_codex._lifecycle import _SyncRun
from tests._asyncio import capture_cancellation


async def test_context_and_one_same_task_exit() -> None:
    from agents.session import (
        _EnvironmentLease as facade_lease,
        _release_environment as facade_release,
        _SyncRun as facade_sync_run,
    )

    assert facade_lease is _EnvironmentLease
    assert facade_release is _release_environment
    assert facade_sync_run is _SyncRun

    scope = ContextVar("scope", default="application")
    entered = []
    exited = []
    environment = object()

    @asynccontextmanager
    async def source():
        entered.append(asyncio.current_task())
        token = scope.set("source")
        try:
            yield environment
        finally:
            assert scope.get() == "source"
            scope.reset(token)
            exited.append(asyncio.current_task())

    lease = _EnvironmentLease(source())
    assert await lease.acquire() is environment
    assert scope.get() == "application"
    assert not exited
    await _release_environment(lease)
    await _release_environment(lease)
    assert entered == exited and len(exited) == 1
    assert entered[0] is not asyncio.current_task()


async def test_cancelled_entry_joins_late_acquire_rollback() -> None:
    entering, allow, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered = []
    exited = []

    @asynccontextmanager
    async def source():
        entered.append(asyncio.current_task())
        entering.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await allow.wait()
        try:
            yield object()
        finally:
            exited.append(asyncio.current_task())
            released.set()

    lease = _EnvironmentLease(source())
    cancellations: list[asyncio.CancelledError] = []
    work = asyncio.create_task(capture_cancellation(lease.acquire(), cancellations))
    await entering.wait()
    work.cancel("entry")
    await asyncio.sleep(0)
    assert not work.done() and not released.is_set()
    allow.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert [error.args for error in cancellations] == [("entry",)]
    assert entered == exited and len(exited) == 1
    assert lease._ready.cancelled()


async def test_release_waiter_cancellation_joins_source_exit() -> None:
    releasing, allow = asyncio.Event(), asyncio.Event()
    exited = []

    @asynccontextmanager
    async def source():
        owner = asyncio.current_task()
        try:
            yield object()
        finally:
            releasing.set()
            await allow.wait()
            assert asyncio.current_task() is owner
            exited.append(owner)

    lease = _EnvironmentLease(source())
    await lease.acquire()
    cancellations: list[asyncio.CancelledError] = []
    work = asyncio.create_task(capture_cancellation(_release_environment(lease), cancellations))
    await releasing.wait()
    work.cancel("first")
    await asyncio.sleep(0)
    work.cancel("second")
    await asyncio.sleep(0)
    assert not work.done() and not exited
    allow.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert [error.args for error in cancellations] == [("first",)]
    assert len(exited) == 1
    assert lease._borrower is None


async def test_source_interrupt_wakes_borrower_after_retirement_join() -> None:
    acquired, retiring = asyncio.Event(), asyncio.Event()
    allow_retire = asyncio.Event()
    exited = []
    leases = []

    @asynccontextmanager
    async def source():
        try:
            yield object()
        finally:
            exited.append(asyncio.current_task())

    async def borrower():
        lease = _EnvironmentLease(source())
        leases.append(lease)
        await lease.acquire()
        acquired.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as error:
            assert error.args == (lease,)
            assert lease.interrupted and not exited
            retiring.set()
            await allow_retire.wait()
            # Native retirement has been represented by the permit above.
            await _release_environment(lease)

    work = asyncio.create_task(borrower())
    await acquired.wait()
    leases[0]._owner.cancel("source timeout")
    await retiring.wait()
    assert not exited and not work.done()
    allow_retire.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert len(exited) == 1
    assert leases[0]._borrower is None


async def test_entry_failure_keeps_source_rollback_and_primary_error() -> None:
    failure = OSError("synthetic entry failure")
    rolled_back = []

    @asynccontextmanager
    async def source():
        try:
            raise failure
            yield object()
        finally:
            rolled_back.append(asyncio.current_task())

    lease = _EnvironmentLease(source())
    with pytest.raises(OSError) as caught:
        await lease.acquire()
    assert caught.value is failure
    assert rolled_back == [lease._owner]
    assert lease._borrower is None


async def test_release_failure_preserves_waiter_cancellation_context() -> None:
    releasing, allow = asyncio.Event(), asyncio.Event()
    failure = OSError("synthetic exit failure")

    @asynccontextmanager
    async def source():
        yield object()
        releasing.set()
        await allow.wait()
        raise failure

    lease = _EnvironmentLease(source())
    await lease.acquire()
    cancellations: list[asyncio.CancelledError] = []
    work = asyncio.create_task(capture_cancellation(_release_environment(lease), cancellations))
    await releasing.wait()
    work.cancel("caller")
    await asyncio.sleep(0)
    allow.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert [error.args for error in cancellations] == [("caller",)]
    assert cancellations[0].__context__ is failure
    assert lease._borrower is None


def test_retained_sync_owner_recovers_on_original_loop() -> None:
    entered, exited = [], []
    retained: list[Any] = []
    run = _SyncRun()

    @asynccontextmanager
    async def source():
        entered.append(asyncio.current_task())
        try:
            yield object()
        finally:
            exited.append(asyncio.current_task())

    async def opening():
        lease = _EnvironmentLease(source())
        await lease.acquire()
        lease._sync_run = run
        run.deferred += 1
        retained.append(lease)

    run.run(opening())
    assert not run.loop.is_running() and not run.loop.is_closed()
    assert not exited

    async def recover():
        await _release_environment(retained.pop())
        run.deferred -= 1

    run.recover(recover)
    assert entered == exited and len(exited) == 1
    assert run.loop.is_closed()


@pytest.mark.parametrize("source_failure", [False, True])
async def test_session_scope_joins_native_before_source_before_provider(
    source_failure: bool,
) -> None:
    from _agents_codex.session import Session

    order = []
    failure = OSError("synthetic source exit")

    @asynccontextmanager
    async def source():
        yield object()
        order.append("source")
        if source_failure:
            raise failure

    class MockNativeSession(Session):
        async def _aclose(self):
            order.append("native")
            self._resources_retired = True

        async def _retire_provider(self):
            order.append("provider")

    session = object.__new__(MockNativeSession)
    session._resources_retired = False
    session._repair_session = None
    session._environment_lease = _EnvironmentLease(source())
    await session._environment_lease.acquire()
    if source_failure:
        with pytest.raises(OSError) as caught:
            await session._exit_scope()
        assert caught.value is failure
    else:
        await session._exit_scope()
    assert order == ["native", "source", "provider"]
    assert session._environment_lease is None
