"""Check artifact verification and turn cleanup without an executor."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agents import Turn
from examples.supervised_worker import __main__ as worker
from tests._asyncio import timeout


@pytest.mark.parametrize(
    ("name_length", "trailing", "accepted"),
    [(117, b"extra bytes\n", False), (118, b"", True)],
    ids=["reject-trailing-bytes", "accept-complete-long-artifact"],
)
def test_worker_verifies_complete_artifact(
    tmp_path: Path, name_length: int, trailing: bytes, accepted: bool
) -> None:
    active_worker = worker.Worker(tmp_path / ("a" * name_length), False)
    active_worker.workspace.mkdir(parents=True)
    expected = active_worker.expected_text
    (active_worker.workspace / worker.ARTIFACT).write_bytes(expected.encode() + trailing)

    assert active_worker.read_artifact() == (expected if accepted else None)


@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["caller-cancel", "both-fail"])
async def test_worker_preserves_primary_error_when_joining_turn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, cleanup_fails: bool
) -> None:
    observer_started = asyncio.Event()
    observer_closed = asyncio.Event()
    cancel_joined = asyncio.Event()
    primary = ValueError("could not read the turn result")
    cause = LookupError("result source failed")
    cleanup = RuntimeError("could not finish cancellation")

    async def events() -> AsyncIterator[object]:
        observer_started.set()
        try:
            yield await asyncio.Future()
        finally:
            observer_closed.set()

    async def wait_for_attention() -> object:
        await observer_started.wait()
        if cleanup_fails:
            raise primary from cause
        return await asyncio.Future()

    async def cancel() -> None:
        await asyncio.sleep(0)
        cancel_joined.set()
        if cleanup_fails:
            raise cleanup

    turn = cast(
        Turn[str],
        SimpleNamespace(events=events, wait_for_attention=wait_for_attention, cancel=cancel),
    )
    trace = worker.TurnTrace(worker.monotonic())
    async with timeout(5):
        task = asyncio.create_task(
            worker.supervise(worker.Worker(tmp_path, False), turn, trace, timeout=4)
        )
        await observer_started.wait()
        if cleanup_fails:
            with pytest.raises(ValueError) as caught:
                await task
            assert caught.value is primary
            assert caught.value.__cause__ is cause
            assert any(
                record.exc_info and record.exc_info[1] is cleanup for record in caplog.records
            )
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not caplog.records

    assert cancel_joined.is_set()
    assert observer_closed.is_set()
