from __future__ import annotations

import asyncio
import contextlib

import pytest

from agents.run_internal._asyncio_progress import get_function_tool_task_progress_deadline


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_detects_timer_backed_sleep() -> None:
    loop = asyncio.get_running_loop()

    async def _sleeping_task() -> None:
        await asyncio.sleep(0.05)

    task = asyncio.create_task(_sleeping_task())
    await asyncio.sleep(0)

    before = loop.time()
    deadline = get_function_tool_task_progress_deadline(
        task=task,
        task_to_invoke_task={},
        loop=loop,
    )

    assert deadline is not None
    assert before <= deadline <= before + 0.1

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_returns_none_for_external_wait() -> None:
    loop = asyncio.get_running_loop()
    blocker: asyncio.Future[None] = loop.create_future()

    async def _blocked_task() -> None:
        await blocker

    task = asyncio.create_task(_blocked_task())
    await asyncio.sleep(0)

    deadline = get_function_tool_task_progress_deadline(
        task=task,
        task_to_invoke_task={},
        loop=loop,
    )

    assert deadline is None

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_tracked_invoke_task() -> None:
    loop = asyncio.get_running_loop()
    outer_started = asyncio.Event()

    async def _invoke_task() -> None:
        await asyncio.sleep(0.05)

    async def _outer_task() -> None:
        outer_started.set()
        await asyncio.Future()

    invoke_task = asyncio.create_task(_invoke_task())
    outer_task = asyncio.create_task(_outer_task())
    await asyncio.wait_for(outer_started.wait(), timeout=0.2)

    before = loop.time()
    deadline = get_function_tool_task_progress_deadline(
        task=outer_task,
        task_to_invoke_task={outer_task: invoke_task},
        loop=loop,
    )

    assert deadline is not None
    assert before <= deadline <= before + 0.1

    outer_task.cancel()
    invoke_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await outer_task
    with contextlib.suppress(asyncio.CancelledError):
        await invoke_task


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_awaited_child_task() -> None:
    loop = asyncio.get_running_loop()

    async def _parent_task() -> None:
        child = asyncio.create_task(asyncio.sleep(0.05))
        await child

    task = asyncio.create_task(_parent_task())
    await asyncio.sleep(0)

    before = loop.time()
    deadline = get_function_tool_task_progress_deadline(
        task=task,
        task_to_invoke_task={},
        loop=loop,
    )

    assert deadline is not None
    assert before <= deadline <= before + 0.1

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_shielded_child_task() -> None:
    loop = asyncio.get_running_loop()

    async def _shielded_task() -> None:
        child = asyncio.create_task(asyncio.sleep(0.05))
        await asyncio.shield(child)

    task = asyncio.create_task(_shielded_task())
    await asyncio.sleep(0)

    before = loop.time()
    deadline = get_function_tool_task_progress_deadline(
        task=task,
        task_to_invoke_task={},
        loop=loop,
    )

    assert deadline is not None
    assert before <= deadline <= before + 0.1

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_gathered_child_tasks() -> None:
    loop = asyncio.get_running_loop()

    async def _gathered_task() -> None:
        await asyncio.gather(asyncio.sleep(0.05), asyncio.sleep(0.06))

    task = asyncio.create_task(_gathered_task())
    await asyncio.sleep(0)

    before = loop.time()
    deadline = get_function_tool_task_progress_deadline(
        task=task,
        task_to_invoke_task={},
        loop=loop,
    )

    assert deadline is not None
    assert before <= deadline <= before + 0.1

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_timer_backed_future() -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    handle = loop.call_later(0.05, future.set_result, None)

    async def _timer_backed_future_task() -> None:
        await future

    task = asyncio.create_task(_timer_backed_future_task())
    await asyncio.sleep(0)

    before = loop.time()
    deadline = get_function_tool_task_progress_deadline(
        task=task,
        task_to_invoke_task={},
        loop=loop,
    )

    assert deadline is not None
    assert before <= deadline <= before + 0.1

    task.cancel()
    handle.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
