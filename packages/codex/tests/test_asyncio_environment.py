"""Test runners have been retaining real subprocess support on every platform."""

import asyncio
import sys

import pytest

from tests._asyncio import original_cancellation


async def subprocess_roundtrip() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        "print('loop-ready')",
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0
        assert output.splitlines() == [b"loop-ready"]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_pytest_loop_supports_subprocesses() -> None:
    await subprocess_roundtrip()


def test_asyncio_run_supports_subprocesses() -> None:
    asyncio.run(subprocess_roundtrip())


async def test_task_await_preserves_original_cancellation_identity() -> None:
    entered = asyncio.Event()
    received = []

    async def body() -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            received.append(error)
            raise

    task = asyncio.create_task(body())
    await entered.wait()
    task.cancel("first cancellation")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert task.cancelled()
    assert original_cancellation(caught.value) is received[0]
    assert received[0].args == ("first cancellation",)
