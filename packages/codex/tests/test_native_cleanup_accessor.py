"""Native cleanup owners survive cancellation error contexts."""

import asyncio
from typing import Any, cast

import pytest

from agents import NativeProvider, get_cleanup
from agents._lifecycle import _join


def test_get_cleanup_retains_nearest_owner_and_does_not_change_error_chain() -> None:
    nearest = NativeProvider()
    older = NativeProvider()
    original = OSError("cleanup failed")
    cast(Any, original).cleanup = older
    cancelled = asyncio.CancelledError()
    cast(Any, cancelled).cleanup = nearest
    cancelled.__context__ = original
    replacement = asyncio.CancelledError()
    replacement.__context__ = cancelled
    cause = RuntimeError("explicit cause")
    replacement.__cause__ = cause

    assert get_cleanup(replacement) is nearest
    assert replacement.__context__ is cancelled
    assert cancelled.__context__ is original
    assert replacement.__cause__ is cause
    assert not hasattr(replacement, "cleanup")


def test_get_cleanup_ignores_provider_only_and_handles_context_cycles() -> None:
    error = RuntimeError("unrelated failure")
    cast(Any, error).cleanup = object()
    cast(Any, error).provider = NativeProvider()
    context = RuntimeError("cyclic context")
    context.__context__ = error
    error.__context__ = context

    assert get_cleanup(error) is None
    assert error.__context__ is context
    assert context.__context__ is error


async def test_cancelled_cleanup_wait_retains_failure_without_background_error():
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("owned cleanup failed")

    async def cleanup():
        entered.set()
        await release.wait()
        raise failure

    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        owned = asyncio.create_task(cleanup())
        await entered.wait()
        waiting = asyncio.create_task(_join(owned))
        await asyncio.sleep(0)
        waiting.cancel("caller cancelled")
        await asyncio.sleep(0)
        assert not owned.done()
        assert not waiting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await waiting
        retained = caught.value.__context__
        while retained is not None and retained is not failure:
            retained = retained.__context__
        assert retained is failure
        assert owned.done() and not owned.cancelled()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        release.set()
        loop.set_exception_handler(previous)
