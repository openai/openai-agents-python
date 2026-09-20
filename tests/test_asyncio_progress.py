from __future__ import annotations

import asyncio

import pytest

from agents.run_internal._asyncio_progress import get_function_tool_task_progress_deadline


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_detects_timer_backed_sleep() -> None:
    loop = asyncio.get_running_loop()

    started = asyncio.Event()

    async def _sleeping_task() -> None:
        started.set()
        await asyncio.sleep(0.05)

    before = loop.time()
    task = asyncio.create_task(_sleeping_task())
    try:
        await started.wait()
        assert not task.done()

        inspected = loop.time()
        deadline = get_function_tool_task_progress_deadline(
            task=task,
            task_to_invoke_task={},
            loop=loop,
        )

        assert deadline is not None
        assert before + 0.05 <= deadline <= inspected + 0.05

    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_returns_none_for_external_wait() -> None:
    loop = asyncio.get_running_loop()
    blocker: asyncio.Future[None] = loop.create_future()

    started = asyncio.Event()

    async def _blocked_task() -> None:
        started.set()
        await blocker

    task = asyncio.create_task(_blocked_task())
    try:
        await started.wait()
        assert not task.done()
        assert not blocker.done()

        deadline = get_function_tool_task_progress_deadline(
            task=task,
            task_to_invoke_task={},
            loop=loop,
        )

        assert deadline is None

    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_tracked_invoke_task() -> None:
    loop = asyncio.get_running_loop()
    outer_started = asyncio.Event()
    invoke_started = asyncio.Event()

    async def _invoke_task() -> None:
        invoke_started.set()
        await asyncio.sleep(0.05)

    async def _outer_task() -> None:
        outer_started.set()
        await asyncio.Future()

    before = loop.time()
    invoke_task = asyncio.create_task(_invoke_task())
    outer_task = asyncio.create_task(_outer_task())
    try:
        await invoke_started.wait()
        await outer_started.wait()
        assert not outer_task.done()
        assert not invoke_task.done()

        inspected = loop.time()
        deadline = get_function_tool_task_progress_deadline(
            task=outer_task,
            task_to_invoke_task={outer_task: invoke_task},
            loop=loop,
        )

        assert deadline is not None
        assert before + 0.05 <= deadline <= inspected + 0.05

    finally:
        outer_task.cancel()
        invoke_task.cancel()
        await asyncio.gather(outer_task, invoke_task, return_exceptions=True)

    assert outer_task.cancelled()
    assert invoke_task.cancelled()


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_awaited_child_task() -> None:
    loop = asyncio.get_running_loop()

    started = asyncio.Event()

    async def _child_task() -> None:
        started.set()
        await asyncio.sleep(0.05)

    async def _parent_task() -> None:
        await child

    before = loop.time()
    child = asyncio.create_task(_child_task())

    task = asyncio.create_task(_parent_task())
    try:
        await started.wait()
        assert not task.done()
        assert not child.done()

        inspected = loop.time()
        deadline = get_function_tool_task_progress_deadline(
            task=task,
            task_to_invoke_task={},
            loop=loop,
        )

        assert deadline is not None
        assert before + 0.05 <= deadline <= inspected + 0.05

    finally:
        task.cancel()
        child.cancel()
        await asyncio.gather(task, child, return_exceptions=True)

    assert task.cancelled()
    assert child.cancelled()


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_shielded_child_task() -> None:
    loop = asyncio.get_running_loop()

    started = asyncio.Event()

    async def _child_task() -> None:
        started.set()
        await asyncio.sleep(0.05)

    async def _shielded_task() -> None:
        await asyncio.shield(child)

    before = loop.time()
    child = asyncio.create_task(_child_task())

    task = asyncio.create_task(_shielded_task())
    try:
        await started.wait()
        assert not task.done()
        assert not child.done()

        inspected = loop.time()
        deadline = get_function_tool_task_progress_deadline(
            task=task,
            task_to_invoke_task={},
            loop=loop,
        )

        assert deadline is not None
        assert before + 0.05 <= deadline <= inspected + 0.05

    finally:
        task.cancel()
        child.cancel()
        await asyncio.gather(task, child, return_exceptions=True)

    assert task.cancelled()
    assert child.cancelled()


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_gathered_child_tasks() -> None:
    loop = asyncio.get_running_loop()

    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def _child_task(started: asyncio.Event, delay: float) -> None:
        started.set()
        await asyncio.sleep(delay)

    async def _gathered_task() -> None:
        await asyncio.gather(first_child, second_child)

    before = loop.time()
    first_child = asyncio.create_task(_child_task(first_started, 0.05))
    second_child = asyncio.create_task(_child_task(second_started, 0.06))

    task = asyncio.create_task(_gathered_task())
    try:
        await first_started.wait()
        await second_started.wait()
        assert not task.done()
        assert not first_child.done()
        assert not second_child.done()

        inspected = loop.time()
        deadline = get_function_tool_task_progress_deadline(
            task=task,
            task_to_invoke_task={},
            loop=loop,
        )

        assert deadline is not None
        assert before + 0.05 <= deadline <= inspected + 0.05

    finally:
        task.cancel()
        first_child.cancel()
        second_child.cancel()
        await asyncio.gather(task, first_child, second_child, return_exceptions=True)

    assert task.cancelled()
    assert first_child.cancelled()
    assert second_child.cancelled()


@pytest.mark.asyncio
async def test_function_tool_task_progress_deadline_can_follow_timer_backed_future() -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()
    handle: asyncio.TimerHandle | None = None

    started = asyncio.Event()

    async def _timer_backed_future_task() -> None:
        started.set()
        await future

    task = asyncio.create_task(_timer_backed_future_task())
    try:
        await started.wait()
        assert not task.done()
        assert not future.done()

        # Arm the real timer after startup so no loop turn can expire it before inspection.
        handle = loop.call_later(0.05, future.set_result, None)
        deadline = get_function_tool_task_progress_deadline(
            task=task,
            task_to_invoke_task={},
            loop=loop,
        )

        assert deadline is not None
        assert deadline == handle.when()

    finally:
        if handle is not None:
            handle.cancel()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert handle is not None and handle.cancelled()
