"""Native cleanup owners survive cancellation error contexts."""

import asyncio
from typing import Any, cast

from agents import NativeProvider, get_cleanup


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
