"""Shared notifications retain per-call joins without retaining closed loops."""

import asyncio
import contextvars
import errno
import os
import sys
import weakref

import pytest

from _agents_codex import _native_async
from agents import _native

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux shared completion channel")


async def test_shared_channel_reuses_one_descriptor_and_adds_no_task():
    loop = asyncio.get_running_loop()
    before = asyncio.all_tasks()
    await _native.shutdown_telemetry()
    dispatcher = _native_async._dispatchers[loop]()
    assert dispatcher is not None
    descriptor = dispatcher._descriptor
    for _ in range(8):
        await _native.shutdown_telemetry()
        assert _native_async._dispatchers[loop]() is dispatcher
        assert dispatcher._descriptor == descriptor
        assert not dispatcher._pending
    assert asyncio.all_tasks() == before


async def test_shared_channel_completes_concurrent_real_calls():
    results = await asyncio.gather(*(_native.shutdown_telemetry() for _ in range(64)))
    # The unchanged generic bridge converts Rust unit to Python's empty tuple.
    assert results == [()] * 64
    dispatcher = _native_async._dispatchers[asyncio.get_running_loop()]()
    assert dispatcher is not None
    assert not dispatcher._pending


async def test_retired_channel_retries_without_consuming_the_native_call():
    loop = asyncio.get_running_loop()
    await _native.shutdown_telemetry()
    old = _native_async._dispatchers[loop]()
    assert old is not None
    old._channel.close()
    # Enter before the old EOF callback runs, so native registration rejects it.
    await _native.shutdown_telemetry()
    current = _native_async._dispatchers[loop]()
    assert current is not None and current is not old
    assert old._retired
    assert old._poll_handle is None
    old._ready()
    os.fstat(current._descriptor)
    await _native.shutdown_telemetry()


def test_shared_channel_does_not_retain_its_first_callers_context():
    class Owner:
        pass

    async def run():
        assert asyncio.get_running_loop() not in _native_async._dispatchers
        context = contextvars.ContextVar("completion_channel_owner")
        owner = Owner()
        reference = weakref.ref(owner)
        token = context.set(owner)
        try:
            await _native.shutdown_telemetry()
        finally:
            context.reset(token)
        del owner, token
        # Retire per-call callback contexts and deferred native Python references.
        await asyncio.sleep(0)
        await _native.shutdown_telemetry()
        await asyncio.sleep(0)
        assert reference() is None

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


def test_closed_loops_release_channels_even_when_the_loop_objects_are_retained():
    retained_loops = []

    async def run():
        await _native.shutdown_telemetry()
        loop = asyncio.get_running_loop()
        dispatcher = _native_async._dispatchers[loop]()
        assert dispatcher is not None
        return weakref.ref(dispatcher), dispatcher._descriptor

    try:
        for _ in range(8):
            loop = asyncio.new_event_loop()
            retained_loops.append(loop)
            reference, descriptor = loop.run_until_complete(run())
            loop.close()
            assert reference() is None
            assert loop not in _native_async._dispatchers
            with pytest.raises(OSError) as error:
                os.fstat(descriptor)
            assert error.value.errno == errno.EBADF
    finally:
        for loop in retained_loops:
            loop.close()


def test_loop_without_descriptor_callbacks_uses_the_existing_completion_path(monkeypatch):
    loop = asyncio.new_event_loop()
    attempted = []

    def unsupported(descriptor, callback, *args):
        attempted.append(descriptor)
        raise NotImplementedError

    async def run():
        await _native.shutdown_telemetry()

    monkeypatch.setattr(loop, "add_reader", unsupported)
    try:
        loop.run_until_complete(run())
        assert attempted
        assert loop not in _native_async._dispatchers
        for descriptor in attempted:
            with pytest.raises(OSError) as error:
                os.fstat(descriptor)
            assert error.value.errno == errno.EBADF
    finally:
        loop.close()
