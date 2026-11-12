"""Private Python callback execution with admission owned by the caller."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any, cast

from .telemetry import _owned_span


class _FunctionBodyFailure(Exception):
    def __init__(self, original: Exception) -> None:
        super().__init__("Function callback failed")
        self.original = original


@dataclass(frozen=True)
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
