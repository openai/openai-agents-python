"""The Windows native completion transport must not block the event loop."""

import asyncio
import socket
import sys
import weakref
from types import SimpleNamespace

import pytest

from agents import _native_async


@pytest.mark.parametrize("outcome", ["native_error", "lost_acknowledgement", "missing_result"])
async def test_native_rejection_is_distinct_from_lost_publication_acknowledgement(
    outcome,
):
    native_error = RuntimeError("Native validation rejected before publication")

    class CompletedCall:
        def __init__(self):
            self.error = None
            self.cancelled = asyncio.Event()

        def start(self, writer):
            async def complete():
                try:
                    if outcome == "lost_acknowledgement":
                        writer.sendall(b"unexpected receipt")
                    elif outcome == "native_error":
                        self.error = native_error
                finally:
                    writer.close()

            self.operation = asyncio.create_task(complete())

        def cancel(self):
            self.cancelled.set()

        def take_result(self):
            if self.error is not None:
                raise self.error
            raise RuntimeError("native completion has no result")

        def discard_unstarted(self):
            pass

        def discard_result(self):
            self.error = None

    expected = (
        _native_async._NativeCompletionError if outcome == "lost_acknowledgement" else RuntimeError
    )
    with pytest.raises(expected) as caught:
        await asyncio.wait_for(_native_async._await_native_completion(CompletedCall()), 5)
    assert type(caught.value) is expected
    if outcome == "native_error":
        assert caught.value is native_error
    elif outcome == "missing_result":
        assert str(caught.value) == "native completion has no result"


@pytest.mark.parametrize("cancel_after_ready", [False, True])
async def test_native_completion_joins_held_writer_after_result_is_ready(cancel_after_ready):
    ready, release, cancellation_requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
    conversions = []

    class OwnedResult:
        pass

    class HeldWriterCall:
        def start(self, writer):
            async def complete():
                try:
                    self.result = OwnedResult()
                    ready.set()
                    await release.wait()
                finally:
                    writer.close()

            self.operation = asyncio.create_task(complete())

        def cancel(self):
            # The result is ready, but real writer retirement is still held.
            cancellation_requested.set()

        def take_result(self):
            conversions.append("converted result")
            result, self.result = self.result, None
            return result

        def discard_unstarted(self):
            pass

        def discard_result(self):
            self.result = None

    call = HeldWriterCall()
    task = asyncio.create_task(_native_async._await_native_completion(call))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        owned_result = weakref.ref(call.result)
        assert not task.done()
        if cancel_after_ready:
            task.cancel()
            await asyncio.wait_for(cancellation_requested.wait(), 5)
            assert not task.done()
            task.cancel()
            # Let the accepted second cancellation run through the event loop;
            # it still cannot return while the actual writer is open.
            progressed = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(progressed.set_result, None)
            await asyncio.wait_for(progressed, 5)
            assert not task.done()
            assert owned_result() is not None
            assert conversions == []
        release.set()
        if cancel_after_ready:
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 5)
            # Retaining the cancelled Task and its exception must not retain
            # the ready result, or cause conversion side effects.
            assert caught.value is not None
            assert task.done()
            assert owned_result() is None
            assert conversions == []
        else:
            assert await asyncio.wait_for(task, 5) is owned_result()
            assert conversions == ["converted result"]
    finally:
        release.set()
        await asyncio.gather(task, call.operation, return_exceptions=True)


@pytest.mark.serial
async def test_windows_completion_pair_closes_sockets_after_eof(monkeypatch):
    allocated = []

    def open_socket(*args, **kwargs):
        sock = socket.socket(*args, **kwargs)
        if allocated:
            # Reserve a port with no unrelated TIME_WAIT connections.
            sock.bind(("127.0.0.1", 0))
        allocated.append(sock)
        return sock

    monkeypatch.setattr(
        _native_async,
        "socket",
        SimpleNamespace(
            socket=open_socket, SOL_SOCKET=socket.SOL_SOCKET, SO_LINGER=socket.SO_LINGER
        ),
    )
    reader, writer = await asyncio.wait_for(_native_async._windows_completion_pair(), 5)
    try:
        assert reader.family == writer.family == socket.AF_INET
        assert allocated[0].fileno() == -1
        writer_address = writer.getsockname()
        reader.setblocking(False)
        writer.close()
        assert await asyncio.wait_for(asyncio.get_running_loop().sock_recv(reader, 1), 5) == b""
    finally:
        reader.close()
        writer.close()
    assert reader.fileno() == writer.fileno() == -1
    assert all(sock.fileno() == -1 for sock in allocated)
    # Other kernels may retain TCP state after the descriptors close. Only Windows
    # uses this transport in production and requires immediate address reuse.
    if sys.platform == "win32":
        with socket.socket() as replacement:
            replacement.bind(writer_address)


@pytest.mark.parametrize("stage", ["sock_connect", "sock_accept"])
@pytest.mark.parametrize("outcome", ["cancelled", "setup_failed"])
async def test_windows_completion_setup_releases_unstarted_native_owner(
    monkeypatch, stage, outcome
):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    allocated = []

    def open_socket(*args, **kwargs):
        sock = socket.socket(*args, **kwargs)
        allocated.append(sock)
        return sock

    release = asyncio.Event()

    async def stalled(*args, **kwargs):
        entered.set()
        await release.wait()
        raise OSError("completion socket setup failed")

    class NativeOwner:
        pass

    class UnstartedCall:
        def __init__(self):
            self.owner = NativeOwner()

        def discard_unstarted(self):
            self.owner = None

        def start(self, writer):
            raise AssertionError("native work started before its completion sockets were connected")

    # Patch only the completion owner's references. The event loop must keep its real
    # platform and socket implementation while the selected step is held.
    monkeypatch.setattr(_native_async, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        _native_async,
        "socket",
        SimpleNamespace(
            socket=open_socket, SOL_SOCKET=socket.SOL_SOCKET, SO_LINGER=socket.SO_LINGER
        ),
    )
    monkeypatch.setattr(loop, stage, stalled)
    call = UnstartedCall()
    owner = weakref.ref(call.owner)
    task = asyncio.create_task(_native_async._await_native_completion(call))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        progressed = loop.create_future()
        loop.call_soon(progressed.set_result, None)
        await asyncio.wait_for(progressed, 5)
        assert not task.done()
        assert len(allocated) == 2
        assert all(sock.fileno() != -1 for sock in allocated)
        if outcome == "cancelled":
            task.cancel()
            expected = asyncio.CancelledError
        else:
            release.set()
            expected = OSError
        with pytest.raises(expected) as caught:
            await asyncio.wait_for(task, 5)
        # Keep the task, exception, traceback and call alive. Cleanup must not
        # rely on Python collection to release the native future's engine owner.
        assert caught.value.__traceback__ is not None
        assert task.done()
        assert owner() is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert all(sock.fileno() == -1 for sock in allocated)
