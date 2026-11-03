"""Real native completion receipts must retire before ownership is released."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents import _native, _native_async
from tests._asyncio import timeout
from tests.native_fixtures import (
    native_engine,
    native_options,
    responses as responses,
)


class ReceiptGate:
    """Keep one selected real NativeCall's completion socket open.

    NativeCall starts, cancels, and consumes its real result as usual. A
    duplicate writer delays that call's actual EOF; it cannot delay another
    call or stand in for a result or a retained Rust writer ticket.
    """

    def __init__(self, monkeypatch, selected=None):
        self.started = asyncio.Event()
        self.armed = False
        self.writer = None
        original = _native_async._await_native_completion
        gate = self

        async def observe(call):
            if self.armed and (selected is None or selected(asyncio.current_task())):
                self.armed = False

                class SelectedCall:
                    def start(self, writer):
                        gate.writer = writer.dup()
                        call.start(writer)
                        gate.started.set()

                    def cancel(self):
                        call.cancel()

                    def take_result(self):
                        return call.take_result()

                    def discard_unstarted(self):
                        return call.discard_unstarted()

                    def discard_result(self):
                        return call.discard_result()

                try:
                    return await original(SelectedCall())
                finally:
                    gate.release()
            return await original(call)

        monkeypatch.setattr(_native_async, "_await_native_completion", observe)

    def release(self):
        self.armed = False
        if self.writer is not None:
            self.writer.close()


def options_for(tmp_path, responses, name):
    directory = tmp_path / name
    directory.mkdir()
    return native_options(directory, responses)


async def test_native_close_does_not_wait_for_its_callers_executor(
    tmp_path, responses, monkeypatch
):
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    async with native_engine(options_for(tmp_path, responses, "client")) as client:
        gate = ReceiptGate(monkeypatch)
        submitted = []

        async def close():
            gate.armed = True
            await client.close()
            return "closed"

        def synchronous_caller():
            future = asyncio.run_coroutine_threadsafe(close(), loop)
            submitted.append(future)
            # Deadlock watchdog, not a timed ordering assertion.
            return future.result(timeout=5)

        caller = loop.run_in_executor(None, synchronous_caller)
        try:
            await asyncio.wait_for(gate.started.wait(), 10)
            assert not caller.done()
            assert gate.writer.fileno() >= 0
            gate.release()
            assert await caller == "closed"
            assert gate.writer.fileno() == -1
        finally:
            gate.release()
            await asyncio.gather(caller, return_exceptions=True)
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in submitted),
                return_exceptions=True,
            )


async def test_native_close_joins_receipt_after_executor_shutdown(tmp_path, responses, monkeypatch):
    loop = asyncio.get_running_loop()
    async with native_engine(options_for(tmp_path, responses, "client")) as client:
        await loop.shutdown_default_executor()
        gate = ReceiptGate(monkeypatch)
        gate.armed = True
        close = asyncio.create_task(client.close())
        try:
            await asyncio.wait_for(gate.started.wait(), 10)
            # Observe loop progress after receipt setup, without using the
            # default executor or elapsed time as a completion assertion.
            reached = loop.create_future()
            loop.call_soon(reached.set_result, None)
            await reached
            assert not close.done()
            assert gate.writer.fileno() >= 0
            gate.release()
            await close
            assert gate.writer.fileno() == -1
        finally:
            gate.release()
            await asyncio.gather(close, return_exceptions=True)


async def test_cancelling_all_owned_tasks_preserves_receipt_join(monkeypatch):
    loop = asyncio.get_running_loop()
    gate = ReceiptGate(monkeypatch)
    receiving = asyncio.Event()
    original_recv = loop.sock_recv

    async def receive(sock, size):
        receiving.set()
        return await original_recv(sock, size)

    monkeypatch.setattr(loop, "sock_recv", receive)
    before = asyncio.all_tasks()
    gate.armed = True
    operation = asyncio.create_task(_native.shutdown_telemetry())
    try:
        async with timeout(10):
            await gate.started.wait()
            await receiving.wait()
            # Reproduce asyncio.run's cancellation of all tasks this operation
            # actually owns. The bridge may receive directly in its caller.
            owned = asyncio.all_tasks() - before
            assert operation in owned
            for task in owned:
                task.cancel()
            # Another real public native operation must still make progress.
            await _native.shutdown_telemetry()
            assert not operation.done()
            assert gate.writer.fileno() >= 0
            gate.release()
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert gate.writer.fileno() == -1
    finally:
        gate.release()
        await asyncio.gather(operation, return_exceptions=True)
