from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from agents.sandbox.materialization import gather_in_order


@pytest.mark.asyncio
async def test_gather_in_order_limits_concurrency_and_preserves_order() -> None:
    active_tasks = 0
    max_active_tasks = 0
    release_tasks = asyncio.Event()
    started_tasks: list[int] = []

    def task_factory(index: int) -> Callable[[], Awaitable[str]]:
        async def run() -> str:
            nonlocal active_tasks
            nonlocal max_active_tasks
            active_tasks += 1
            max_active_tasks = max(max_active_tasks, active_tasks)
            started_tasks.append(index)
            try:
                await release_tasks.wait()
                return f"result-{index}"
            finally:
                active_tasks -= 1

        return run

    gather_task = asyncio.create_task(
        gather_in_order([task_factory(index) for index in range(5)], max_concurrency=2)
    )
    while len(started_tasks) < 2:
        await asyncio.sleep(0)

    assert started_tasks == [0, 1]
    assert max_active_tasks == 2

    release_tasks.set()
    result = await gather_task

    assert result == ["result-0", "result-1", "result-2", "result-3", "result-4"]
    assert max_active_tasks == 2


@pytest.mark.asyncio
async def test_gather_in_order_rejects_invalid_concurrency() -> None:
    with pytest.raises(ValueError) as exc_info:
        await gather_in_order([], max_concurrency=0)

    assert str(exc_info.value) == "max_concurrency must be at least 1"
