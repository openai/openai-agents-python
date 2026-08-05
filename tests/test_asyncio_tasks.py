from __future__ import annotations

import asyncio

import pytest

from agents.util._asyncio_tasks import gather_with_cancel


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_gather_with_cancel_reports_child_failure_before_cancelling_siblings(
    error_type: type[BaseException],
) -> None:
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    child_failure_reported = asyncio.Event()

    async def sibling() -> None:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    async def fail_after_sibling_starts() -> None:
        await sibling_started.wait()
        raise error_type("child failed")

    with pytest.raises(error_type):
        await gather_with_cancel(
            sibling(),
            fail_after_sibling_starts(),
            on_child_failure=child_failure_reported.set,
        )

    assert child_failure_reported.is_set()
    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_gather_with_cancel_does_not_report_parent_cancellation_as_child_failure() -> None:
    children_started = 0
    all_children_started = asyncio.Event()
    child_failure_reported = asyncio.Event()
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()

    async def child() -> None:
        nonlocal children_started
        children_started += 1
        if children_started == 2:
            all_children_started.set()
        await asyncio.Event().wait()

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        task = asyncio.create_task(
            gather_with_cancel(
                child(),
                child(),
                on_child_failure=child_failure_reported.set,
            )
        )
        await all_children_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_exception_handler)

    assert not child_failure_reported.is_set()
    assert loop_errors == []
