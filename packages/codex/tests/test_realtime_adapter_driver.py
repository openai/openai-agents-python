"""Application I/O ownership behind the native adapter request capability."""

import asyncio
import json
from contextlib import nullcontext
from typing import Any

import pytest

from _agents_codex.realtime_adapter import (
    RealtimeAdapterContext,
    _RealtimeAdapterDriver,
)
from tests._asyncio import timeout


class Request:
    def __init__(self, event: dict[str, Any]) -> None:
        self.event_json = json.dumps(event)
        self.result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def complete(self, success: bool) -> bool:
        if self.result.done():
            return False
        self.result.set_result(success)
        return True


class Handle:
    def __init__(self) -> None:
        self.requests: asyncio.Queue[Request | None] = asyncio.Queue()
        self.frames: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.ended: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def next(self) -> Request | None:
        return await self.requests.get()

    async def receive(self, frame: str) -> None:
        self.frames.append(frame)

    def end(self, success: bool) -> None:
        if not self.ended.done():
            self.ended.set_result(success)

    def finish(self) -> None:
        self.requests.put_nowait(None)

    def request(self, kind: str, **params: Any) -> asyncio.Future[bool]:
        event = {"type": kind, **params}
        self.events.append(event)
        request = Request(event)
        self.requests.put_nowait(request)
        return request.result


class Connection:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        self.sending = asyncio.Event()
        self.release_send = asyncio.Event()
        self.close_count = 0
        self.close_error: Exception | None = None
        self.incoming = self.iter_frames()

    async def iter_frames(self):
        while (frame := await self.frames.get()) is not None:
            yield frame

    async def send(self, frame: str) -> None:
        self.sending.set()
        await self.release_send.wait()

    async def aclose(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            error, self.close_error = self.close_error, None
            raise error
        self.release_send.set()
        self.frames.put_nowait(None)


class Adapter:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def connect(self, context: RealtimeAdapterContext) -> Connection:
        return self.connection


def driver(adapter: Any, handle: Handle, errors: list[BaseException]) -> _RealtimeAdapterDriver:
    owner = _RealtimeAdapterDriver(
        adapter, handle, on_error=errors.append, callback_scope=nullcontext
    )
    owner.start()
    return owner


def connect(handle: Handle) -> asyncio.Future[bool]:
    return handle.request("connect", url="wss://example.invalid/realtime", headers={}, model=None)


async def test_pending_send_does_not_block_native_cancel_or_close() -> None:
    handle, connection, errors = Handle(), Connection(), []
    owner = driver(Adapter(connection), handle, errors)
    async with timeout(2):
        assert await connect(handle)
        sending = handle.request("send", frame='{"type":"session.update"}')
        await connection.sending.wait()
        assert await handle.request("cancel")
        assert await handle.request("close")
        assert not await sending
        await owner.aclose()
    assert connection.close_count == 1
    assert errors == []


async def test_late_factory_result_is_closed_before_cleanup_completes() -> None:
    handle, connection, errors = Handle(), Connection(), []
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class LateAdapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return connection
            raise AssertionError("unreachable")

    owner = driver(LateAdapter(), handle, errors)
    async with timeout(2):
        opening = connect(handle)
        await started.wait()
        assert await handle.request("cancel")
        await cancelled.wait()
        closing = handle.request("close")
        assert not closing.done()
        release.set()
        assert await closing
        assert not await opening
        await owner.aclose()
    assert connection.close_count == 1
    assert errors == []


@pytest.mark.parametrize("after_native_retirement", [False, True])
async def test_failed_cleanup_retains_connection_for_retry(after_native_retirement: bool) -> None:
    handle, connection, errors = Handle(), Connection(), []
    failure = RuntimeError("private cleanup failure")
    connection.close_error = failure
    owner = driver(Adapter(connection), handle, errors)
    async with timeout(2):
        assert await connect(handle)
        if after_native_retirement:
            with pytest.raises(RuntimeError) as caught:
                await owner.aclose()
            assert caught.value is failure
        else:
            assert not await handle.request("close")
        assert owner.cleanup_error is failure
        if not after_native_retirement:
            assert await handle.request("close")
        await owner.aclose()
    assert owner.cleanup_error is None
    assert connection.close_count == 2


async def test_factory_error_stays_local_while_native_receives_only_status() -> None:
    handle, errors = Handle(), []
    failure = RuntimeError("private application exception")

    class FailingAdapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            raise failure

    owner = driver(FailingAdapter(), handle, errors)
    async with timeout(2):
        assert not await connect(handle)
        await owner.aclose()
    assert owner.error is failure
    assert errors == [failure]
    assert str(failure) not in json.dumps(handle.events)


@pytest.mark.parametrize(
    "frame", [42, "x" * (8 * 1024 * 1024 + 1)], ids=["non-text", "oversized-text"]
)
async def test_invalid_ingress_is_rejected_before_native_copy(frame: Any) -> None:
    handle, connection, errors = Handle(), Connection(), []
    owner = driver(Adapter(connection), handle, errors)
    async with timeout(2):
        assert await connect(handle)
        connection.frames.put_nowait(frame)
        assert not await handle.ended
        assert await handle.request("close")
        await owner.aclose()
    assert handle.frames == []
    assert isinstance(owner.error, (TypeError, ValueError))


async def test_cancel_before_factory_dispatch_settles_the_request() -> None:
    handle, errors = Handle(), []
    entered = False

    class UnusedAdapter:
        async def connect(self, context: RealtimeAdapterContext) -> Connection:
            nonlocal entered
            entered = True
            return Connection()

    owner = driver(UnusedAdapter(), handle, errors)
    opening = connect(handle)
    cancelled = handle.request("cancel")
    closing = handle.request("close")
    async with timeout(2):
        assert await cancelled
        assert await closing
        assert not await opening
        await owner.aclose()
    assert not entered
    assert errors == []


async def test_retirement_joins_first_failed_close_before_explicit_retry() -> None:
    handle, connection, errors = Handle(), Connection(), []
    failure = RuntimeError("first physical failure")
    connection.close_error = failure
    owner = driver(Adapter(connection), handle, errors)
    async with timeout(2):
        assert await connect(handle)
        assert not await handle.request("close")
        handle.finish()
        with pytest.raises(RuntimeError) as caught:
            await owner.aclose()
        assert caught.value is failure
        assert connection.close_count == 1
        assert owner.cleanup_error is failure
        await owner.aclose()
        assert connection.close_count == 2
        assert owner.cleanup_error is None
