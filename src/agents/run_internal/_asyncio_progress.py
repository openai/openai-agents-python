from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any


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
        if getattr(callback, "__name__", None) == "_set_result_unless_cancelled" and args:
            if args[0] is future:
                return float(handle.when())
    return None


def _iter_future_child_tasks(future: asyncio.Future[Any]) -> tuple[asyncio.Task[Any], ...]:
    """Best-effort extraction of nested tasks that drive this future forward."""
    children = tuple(
        child for child in getattr(future, "_children", ()) if isinstance(child, asyncio.Task)
    )
    if children:
        return children

    callbacks = getattr(future, "_callbacks", None) or ()
    discovered: list[asyncio.Task[Any]] = []
    for callback_entry in callbacks:
        callback = callback_entry[0] if isinstance(callback_entry, tuple) else callback_entry
        for cell in getattr(callback, "__closure__", ()) or ():
            if isinstance(cell.cell_contents, asyncio.Task):
                discovered.append(cell.cell_contents)
    return tuple(discovered)


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
