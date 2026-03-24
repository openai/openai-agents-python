"""Best-effort progress inspection for cancelled function-tool tasks.

These helpers prefer public coroutine introspection first, then fall back to a
small set of private asyncio attributes for patterns that still hide their
driving tasks or deadlines (`Task._fut_waiter`, gather `_children`, shield
callbacks, and loop `_scheduled`). When a structure is not recognized, the
helpers must fail safe by returning ``None`` rather than raising.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any


def _get_awaitable_to_wait_on(awaitable: Any) -> Any | None:
    """Return the next awaitable in a coroutine/generator chain, if public APIs expose it."""
    if inspect.iscoroutine(awaitable):
        return awaitable.cr_await
    if inspect.isgenerator(awaitable):
        return awaitable.gi_yieldfrom
    if inspect.isasyncgen(awaitable):
        return awaitable.ag_await
    return None


def _get_sleep_deadline_from_awaitable(
    awaitable: Any,
    *,
    loop: asyncio.AbstractEventLoop,
) -> float | None:
    """Return the wake-up deadline for asyncio.sleep-style awaitables when visible."""
    if inspect.isgenerator(awaitable):
        code = getattr(awaitable, "gi_code", None)
        if code is not None and code.co_name == "__sleep0":
            return loop.time()
        return None

    if not inspect.iscoroutine(awaitable):
        return None

    frame = awaitable.cr_frame
    if frame is None or frame.f_code.co_name != "sleep":
        return None

    handle = frame.f_locals.get("h")
    when = getattr(handle, "when", None)
    if callable(when):
        return float(when())

    delay = frame.f_locals.get("delay")
    if isinstance(delay, (int, float)):
        return loop.time() if delay <= 0 else loop.time() + float(delay)
    return None


def _get_scheduled_future_deadline(
    loop: asyncio.AbstractEventLoop,
    future: asyncio.Future[Any],
) -> float | None:
    """Return the next loop deadline for a timer-backed future, if any."""
    scheduled_handles = getattr(loop, "_scheduled", None)
    if not scheduled_handles:
        return None

    for handle in scheduled_handles:
        if handle.cancelled():
            continue
        callback = getattr(handle, "_callback", None)
        args = getattr(handle, "_args", ())
        callback_self = getattr(callback, "__self__", None)
        callback_name = getattr(callback, "__name__", None)
        if callback_self is future and callback_name in {"cancel", "set_exception", "set_result"}:
            return float(handle.when())
        if getattr(callback, "__name__", None) == "_set_result_unless_cancelled" and args:
            if args[0] is future:
                return float(handle.when())
    return None


def _iter_shielded_future_child_tasks(future: asyncio.Future[Any]) -> tuple[asyncio.Task[Any], ...]:
    """Return child tasks captured by asyncio.shield callbacks, if recognizable."""
    callbacks = getattr(future, "_callbacks", None) or ()
    discovered: list[asyncio.Task[Any]] = []
    for callback_entry in callbacks:
        callback = callback_entry[0] if isinstance(callback_entry, tuple) else callback_entry
        if getattr(callback, "__name__", None) != "_outer_done_callback":
            continue
        for cell in getattr(callback, "__closure__", ()) or ():
            if isinstance(cell.cell_contents, asyncio.Task):
                discovered.append(cell.cell_contents)
    return tuple(discovered)


def _iter_future_child_tasks(future: asyncio.Future[Any]) -> tuple[asyncio.Task[Any], ...]:
    """Best-effort extraction of nested tasks that drive this future forward."""
    children = tuple(
        child for child in getattr(future, "_children", ()) if isinstance(child, asyncio.Task)
    )
    if children:
        return children
    return _iter_shielded_future_child_tasks(future)


def _get_self_progress_deadline_for_future(
    future: asyncio.Future[Any],
    *,
    loop: asyncio.AbstractEventLoop,
    seen: set[int],
) -> float | None:
    """Return when a future can make progress without outside input, if determinable."""
    future_id = id(future)
    if future_id in seen:
        return None
    seen.add(future_id)

    if future.done():
        return loop.time()

    if isinstance(future, asyncio.Task):
        public_deadline = _get_self_progress_deadline_for_awaitable(
            future.get_coro(),
            loop=loop,
            seen=seen,
        )
        if public_deadline is not None:
            return public_deadline

        waiter = getattr(future, "_fut_waiter", None)
        if waiter is None:
            return loop.time()
        return _get_self_progress_deadline_for_future(waiter, loop=loop, seen=seen)

    child_tasks = _iter_future_child_tasks(future)
    if child_tasks:
        pending_child_tasks = [child for child in child_tasks if not child.done()]
        if not pending_child_tasks:
            return loop.time()
        child_deadlines = [
            _get_self_progress_deadline_for_future(child, loop=loop, seen=seen)
            for child in pending_child_tasks
        ]
        ready_deadlines = [deadline for deadline in child_deadlines if deadline is not None]
        return min(ready_deadlines) if ready_deadlines else None

    return _get_scheduled_future_deadline(loop, future)


def _get_self_progress_deadline_for_awaitable(
    awaitable: Any,
    *,
    loop: asyncio.AbstractEventLoop,
    seen: set[int],
) -> float | None:
    """Follow public awaitable chains before falling back to future-specific probing."""
    if awaitable is None:
        return loop.time()

    awaitable_id = id(awaitable)
    if awaitable_id in seen:
        return None
    seen.add(awaitable_id)

    sleep_deadline = _get_sleep_deadline_from_awaitable(awaitable, loop=loop)
    if sleep_deadline is not None:
        return sleep_deadline

    if isinstance(awaitable, asyncio.Future):
        return _get_self_progress_deadline_for_future(awaitable, loop=loop, seen=seen)

    next_awaitable = _get_awaitable_to_wait_on(awaitable)
    if next_awaitable is None:
        return None
    return _get_self_progress_deadline_for_awaitable(next_awaitable, loop=loop, seen=seen)


def get_function_tool_task_progress_deadline(
    *,
    task: asyncio.Task[Any],
    task_to_invoke_task: Mapping[asyncio.Task[Any], asyncio.Task[Any]],
    loop: asyncio.AbstractEventLoop,
) -> float | None:
    """Return the next self-driven progress deadline for a cancelled function-tool task."""
    task_waiter = getattr(task, "_fut_waiter", None)
    if task_waiter is not None and task_waiter.done():
        return loop.time()
    tracked_task = task_to_invoke_task.get(task)
    target_task = tracked_task if tracked_task is not None and not tracked_task.done() else task
    return _get_self_progress_deadline_for_future(target_task, loop=loop, seen=set())
