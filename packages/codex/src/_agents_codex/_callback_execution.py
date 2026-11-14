"""Private Python callback execution with admission owned by the caller."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass as dataclass
from typing import Any, cast

from ._compat import cancel_once
from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._lifecycle import _close_after_error, _join
from .telemetry import _owned_span


class _FunctionBodyFailure(Exception):
    def __init__(self, original: Exception) -> None:
        super().__init__("Function callback failed")
        self.original = original


@_sdk_dataclass(frozen=True)
class _RecoverableFunctionCallback:
    callback: Callable[..., Any]


async def _call_callback(
    callback: Callable[..., Any] | _RecoverableFunctionCallback,
    admit: Callable[[], bool],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    try:
        return await _call_callback_entered(callback, admit, args, kwargs)
    finally:
        del callback, admit, args, kwargs


async def _call_callback_entered(
    callback: Callable[..., Any] | _RecoverableFunctionCallback,
    admit: Callable[[], bool],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    enter: Callable[[], None] | None = None,
) -> Any:
    try:
        with _owned_span("callback", "callback"):
            return await _call_callback_body(callback, admit, args, kwargs, enter)
    finally:
        # Release SDK-owned callback arguments only after the admitted body exits.
        del callback, admit, args, kwargs, enter


async def _call_application_callback(
    callback: Callable[..., Any] | _RecoverableFunctionCallback,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
    timeout_error: TimeoutError | None = None,
) -> Any:
    """Own a callback without a Session, including any worker that entered it."""
    loop = asyncio.get_running_loop()
    cutoff = None if timeout_seconds is None else loop.time() + timeout_seconds
    active = True
    settled_at: float | None = None
    control_error: SystemExit | KeyboardInterrupt | None = None

    def admit() -> bool:
        return active and (cutoff is None or loop.time() < cutoff)

    async def run() -> Any:
        nonlocal settled_at, control_error
        try:
            if admit():
                return await _call_callback_entered(callback, admit, args, kwargs)
            return None
        except (SystemExit, KeyboardInterrupt) as error:
            # A child asyncio task must not propagate these to the event loop.
            control_error = error
            return None
        finally:
            settled_at = loop.time()

    pending = asyncio.create_task(run())

    def revoke() -> None:
        nonlocal active
        if active and not pending.done():
            active = False
            cancel_once(pending)

    timer = None if cutoff is None else loop.call_at(cutoff, revoke)
    try:
        try:
            await asyncio.wait((pending,))
        except asyncio.CancelledError as error:
            revoke()

            async def join() -> None:
                try:
                    await _join(pending)
                except asyncio.CancelledError:
                    if not pending.cancelled():
                        raise

            await _close_after_error(error, join)
            raise
        if control_error is not None:
            raise control_error
        if cutoff is not None and (not active or settled_at is not None and settled_at >= cutoff):
            failure = None if pending.cancelled() else pending.exception()
            if isinstance(failure, _FunctionBodyFailure):
                failure = failure.original
            assert timeout_error is not None
            raise timeout_error from failure
        return pending.result()
    finally:
        if timer is not None:
            timer.cancel()
        args = ()
        kwargs = {}
        callback = pending = cast(Any, None)
        control_error = None


async def _call_callback_body(
    callback: Callable[..., Any] | _RecoverableFunctionCallback,
    admit: Callable[[], bool],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    enter: Callable[[], None] | None = None,
) -> Any:
    """Run Python in its appropriate executor and retain ownership until exit."""
    if isinstance(callback, _RecoverableFunctionCallback):
        function = callback.callback
        capture_error = True
    else:
        function = callback
        capture_error = False
    worker: asyncio.Future[Any] | None = None
    value: Any = None
    try:
        if inspect.iscoroutinefunction(function):
            if enter is not None:
                if not admit():
                    return None
                enter()
            try:
                return await function(*args, **kwargs)
            except Exception as error:
                if capture_error:
                    raise _FunctionBodyFailure(error) from error
                raise
        loop = asyncio.get_running_loop()

        async def admission() -> bool:
            # The asyncio and native ownership check belongs to the application's loop.
            allowed = admit()
            if allowed and enter is not None:
                enter()
            return allowed

        def invoke_if_admitted() -> Any:
            if asyncio.run_coroutine_threadsafe(admission(), loop).result():
                try:
                    return function(*args, **kwargs)
                except Exception as error:
                    if capture_error:
                        raise _FunctionBodyFailure(error) from error
                    raise
            return None

        worker = loop.run_in_executor(None, copy_context().run, invoke_if_admitted)
        cancelled = None
        while not worker.done():
            try:
                # Waiting must not cancel a running worker. A cancelled shield can
                # also log the raw callback exception before its owner sanitizes it.
                await asyncio.wait((worker,))
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
        value = worker.result()
        if cancelled is not None:
            # A synchronous wrapper may return a coroutine. Cancellation must
            # not start that body after the callback's authority has expired.
            if inspect.iscoroutine(value):
                value.close()
            raise cancelled
        if inspect.isawaitable(value):
            try:
                allowed = admit()
            except BaseException:
                if inspect.iscoroutine(value):
                    value.close()
                raise
            if not allowed:
                # Declined work must not start the coroutine's body on the way out.
                if inspect.iscoroutine(value):
                    value.close()
                return None
            try:
                return await value
            except Exception as error:
                if capture_error:
                    raise _FunctionBodyFailure(error) from error
                raise
        return value
    finally:
        # Release SDK-owned callback arguments only after the admitted body exits.
        args = ()
        kwargs = {}
        enter = None
        callback = function = admit = cast(Any, None)
        del worker, value
