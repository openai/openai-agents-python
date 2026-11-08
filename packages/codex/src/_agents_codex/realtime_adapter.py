"""Application-owned I/O below the native Realtime protocol owner."""

import asyncio
import builtins
import inspect
import json
import sys
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from functools import partial
from types import MappingProxyType
from typing import Protocol

from ._lifecycle import _join
from ._public import preserve_module_identity as _preserve_module_identity


@dataclass(frozen=True)
class RealtimeAdapterContext:
    """Private connection data; the factory may choose its own endpoint and auth."""

    url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    model: str | None = None


class NativeRealtimeConnection(Protocol):
    """Canonical Realtime V2 frames, with independent incoming and outgoing I/O."""

    @property
    def incoming(self) -> AsyncIterator[str]: ...

    async def send(self, frame: str) -> None: ...

    async def aclose(self) -> None:
        """Release I/O and wake pending sends and incoming iteration.

        A failed close can be retried and must be idempotent.
        """
        ...


class NativeRealtimeAdapter(Protocol):
    """Supply a socket or translate another model's protocol to Realtime V2 frames.

    Core retains history, tool authorization, configuration ACKs and ordering.
    The factory must clean up partial resources before raising or cancelling.
    """

    async def connect(self, context: RealtimeAdapterContext) -> NativeRealtimeConnection: ...


class _AdapterRequest(Protocol):
    @property
    def event_json(self) -> str: ...

    def complete(self, success: bool) -> bool: ...


class _AdapterHandle(Protocol):
    async def next(self) -> _AdapterRequest | None: ...

    async def receive(self, frame: str) -> None: ...

    def end(self, success: bool) -> None: ...

    def finish(self) -> None: ...


def _clear_adapter_error_frames(error: BaseException) -> None:
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback.clear_frames(current.__traceback__)
        if sys.version_info >= (3, 11):
            if isinstance(current, builtins.BaseExceptionGroup):
                pending.extend(current.exceptions)
        pending.extend(
            item for item in (current.__cause__, current.__context__) if item is not None
        )


@dataclass
class _OwnedConnection:
    close: Callable[[], Awaitable[None]]
    send: Callable[[str], Awaitable[None]] | None = None
    incoming: AsyncIterator[str] | None = None


class _RealtimeAdapterDriver:
    """Own application I/O while native code retains protocol interpretation."""

    def __init__(
        self,
        adapter: NativeRealtimeAdapter,
        handle: _AdapterHandle,
        *,
        on_error: Callable[[BaseException], None],
        callback_scope: Callable[[], AbstractContextManager[None]],
    ) -> None:
        self._connect = adapter.connect
        self._handle = handle
        self._on_error = on_error
        self._callback_scope = callback_scope
        self._running: asyncio.Task[None] | None = None
        self._setup: asyncio.Task[None] | None = None
        self._reading: asyncio.Task[None] | None = None
        self._sends: set[asyncio.Task[None]] = set()
        self._controls: set[asyncio.Task[None]] = set()
        self._connection: _OwnedConnection | None = None
        self._cleaning: asyncio.Task[None] | None = None
        self._cancelled = False
        self._cleaned = False
        self._finished = False
        self.error: BaseException | None = None
        self.cleanup_error: BaseException | None = None

    def start(self) -> None:
        if self._running is not None:
            raise RuntimeError("Realtime adapter driver already started")
        self._running = asyncio.create_task(self._run())
        self._running.add_done_callback(self._observe)

    @staticmethod
    def _observe(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _settle_cancelled(request: _AdapterRequest, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            request.complete(False)

    def _remember(self, error: BaseException) -> None:
        if self.error is None:
            self.error = error
            self._on_error(error)

    def _cancel_io(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        for task in (self._setup, self._reading, *self._sends):
            if task is not None and not task.done():
                task.cancel()

    async def _run(self) -> None:
        try:
            while (request := await self._handle.next()) is not None:
                try:
                    event = json.loads(request.event_json)
                    kind = event["type"]
                    if kind == "connect":
                        if self._setup is not None:
                            raise RuntimeError("Realtime adapter was connected twice")
                        self._setup = asyncio.create_task(
                            self._open(
                                request,
                                RealtimeAdapterContext(
                                    url=event["url"],
                                    headers=MappingProxyType(dict(event["headers"])),
                                    model=event.get("model"),
                                ),
                            )
                        )
                        self._setup.add_done_callback(partial(self._settle_cancelled, request))
                    elif kind == "send":
                        frame = event["frame"]
                        if not isinstance(frame, str):
                            raise TypeError("Invalid adapter frame")
                        task = asyncio.create_task(self._send(request, frame))
                        self._sends.add(task)
                        task.add_done_callback(partial(self._settle_cancelled, request))
                        task.add_done_callback(self._sends.discard)
                    elif kind == "close":
                        task = asyncio.create_task(self._close_request(request))
                        self._controls.add(task)
                        task.add_done_callback(self._controls.discard)
                    elif kind == "cancel":
                        self._cancel_io()
                        request.complete(True)
                    else:
                        raise TypeError("Unknown native adapter request")
                except BaseException as error:
                    self._remember(error)
                    request.complete(False)
                    self._handle.end(False)
                finally:
                    # Setup owns its request/context; the reader need not retain credentials.
                    event = None
                    frame = None
                    request = None
        except BaseException as error:
            self._remember(error)
        finally:
            await self._cleanup()
            if self._controls:
                await asyncio.gather(*self._controls)

    async def _open(self, request: _AdapterRequest, context: RealtimeAdapterContext) -> None:
        try:
            if self._cancelled:
                request.complete(False)
                return
            with self._callback_scope():
                connection = await self._connect(context)
                close = connection.aclose
                if not callable(close):
                    raise TypeError("Realtime adapter must return an owned connection")
                self._connection = _OwnedConnection(close=close)
                send = connection.send
                if not callable(send):
                    raise TypeError("Realtime adapter connection requires send")
                self._connection.send = send
                self._connection.incoming = aiter(connection.incoming)
            if self._cancelled:
                request.complete(False)
            elif request.complete(True):
                self._reading = asyncio.create_task(self._read())
        except BaseException as error:
            # clear_frames skips this active frame, so release its private input explicitly.
            del context
            _clear_adapter_error_frames(error)
            if not self._cancelled:
                self._remember(error)
            request.complete(False)
        finally:
            # Preserve error-before-completion ordering, then release the request payload.
            del request

    async def _read(self) -> None:
        assert self._connection is not None and self._connection.incoming is not None
        iterator = self._connection.incoming
        try:
            while not self._cancelled:
                with self._callback_scope():
                    frame = await anext(iterator)
                try:
                    if not isinstance(frame, str):
                        raise TypeError("Realtime adapter incoming frames must be strings")
                    if len(frame) > 8 * 1024 * 1024 or len(frame.encode("utf-8")) > 8 * 1024 * 1024:
                        raise ValueError("Realtime adapter frame exceeds the native limit")
                    if not self._cancelled:
                        await self._handle.receive(frame)
                finally:
                    del frame
        except StopAsyncIteration:
            self._handle.end(True)
        except BaseException as error:
            _clear_adapter_error_frames(error)
            if not self._cancelled:
                self._remember(error)
                self._handle.end(False)

    async def _send(self, request: _AdapterRequest, frame: str) -> None:
        try:
            if self._cancelled:
                request.complete(False)
                return
            if self._connection is None or self._connection.send is None:
                raise RuntimeError("Realtime adapter is not connected")
            with self._callback_scope():
                await self._connection.send(frame)
            request.complete(not self._cancelled)
        except BaseException as error:
            # clear_frames skips this active frame; drop the private wire input first.
            del frame
            _clear_adapter_error_frames(error)
            if not self._cancelled:
                self._remember(error)
            request.complete(False)
        finally:
            del request

    async def _close_request(self, request: _AdapterRequest) -> None:
        try:
            await self._cleanup(retry_failed=True)
        except BaseException:
            request.complete(False)
        else:
            request.complete(True)

    async def _cleanup(self, *, retry_failed: bool = False) -> None:
        if self._cleaned:
            return
        if self._cleaning is None or (
            retry_failed
            and self._cleaning.done()
            and (self._cleaning.cancelled() or self._cleaning.exception() is not None)
        ):
            self._cleaning = asyncio.create_task(self._cleanup_once())
        await _join(self._cleaning)

    async def _cleanup_once(self) -> None:
        self._cancel_io()
        try:
            if self._setup is not None:
                await asyncio.gather(self._setup, return_exceptions=True)
            connection = self._connection
            if connection is not None:
                with self._callback_scope():
                    await connection.close()
            pending = [*self._sends]
            if self._reading is not None:
                pending.append(self._reading)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if connection is not None and connection.incoming is not None:
                with self._callback_scope():
                    close_iterator = getattr(connection.incoming, "aclose", None)
                    if close_iterator is not None:
                        result = close_iterator()
                        if inspect.isawaitable(result):
                            await result
            self._connection = None
            self._cleaned = True
            self.cleanup_error = None
        except BaseException as error:
            self.cleanup_error = error
            raise

    async def aclose(self) -> None:
        """Finish only after native close succeeds; retry failed local cleanup."""
        if self._finished:
            await self._cleanup(retry_failed=True)
            return
        self._handle.finish()
        self._finished = True
        if self._running is not None:
            await _join(self._running)
        else:
            await self._cleanup()


_preserve_module_identity(globals(), "agents.realtime")
