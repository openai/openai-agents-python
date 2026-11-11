"""Join native completion publishers before releasing Python ownership."""

from __future__ import annotations

import asyncio as asyncio
import contextvars as contextvars
import os as os
import socket as socket
import struct as struct
import sys as sys
import weakref as weakref
from contextlib import ExitStack as ExitStack
from typing import Any as Any, cast as cast

from ._public import preserve_module_identity as _preserve_module_identity


class _NativeCompletionError(RuntimeError):
    """The native operation's publication acknowledgement was not confirmed."""


_dispatchers: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[_CompletionDispatcher]
] = weakref.WeakKeyDictionary()


def _unix_completion_pair() -> tuple[socket.socket, socket.socket]:
    reader, writer = socket.socketpair()
    try:
        reader.setblocking(False)
    except BaseException:
        reader.close()
        writer.close()
        raise
    return reader, writer


class _CompletionDispatcher:
    """Keep one notification socket while its loop owns the reader callback."""

    def __init__(self, loop: asyncio.AbstractEventLoop, call: Any) -> None:
        self._loop = weakref.ref(loop)
        self._pending: dict[int, tuple[Any, asyncio.Future[None]]] = {}
        self._next_id = 0
        self._closed = False
        self._retired = False
        self._poll_handle: asyncio.TimerHandle | None = None
        self._reader: socket.socket | None = None
        self._channel: Any = None
        self._descriptor = -1
        try:
            self._reader, writer = _unix_completion_pair()
            self._descriptor = self._reader.fileno()
            try:
                writer.setblocking(False)
                self._channel = call.completion_channel(writer)
            finally:
                writer.close()
            # A persistent callback must not retain its first caller's context.
            contextvars.Context().run(loop.add_reader, self._descriptor, self._ready)
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise

    def _discard_cached(self) -> None:
        loop = self._loop()
        if loop is not None:
            reference = _dispatchers.get(loop)
            if reference is not None:
                target = reference()
                if target is None or target is self:
                    del _dispatchers[loop]

    def _remove_reader(self) -> bool:
        if self._reader is None or self._reader.fileno() < 0:
            return True
        loop = self._loop()
        if loop is not None and not loop.is_closed():
            try:
                loop.remove_reader(self._descriptor)
            except Exception:
                # A retained callback must never touch a recycled descriptor.
                # Keep the socket owned until the loop drops that callback.
                return False
        return True

    def _retire_reader(self) -> bool:
        if not self._remove_reader():
            return False
        if self._reader is not None:
            self._reader.close()
        return True

    def _ready(self) -> None:
        if self._closed:
            return
        if self._retired:
            self._retire_reader()
            return
        assert self._reader is not None
        while True:
            try:
                acknowledgement = self._reader.recv(65536)
                if not acknowledgement:
                    self._retire()
                    return
                break
            except InterruptedError:
                continue
            except BlockingIOError:
                break
            except OSError:
                self._retire()
                return
        # Read before draining the queue. A publisher joins this batch or sees
        # the empty queue afterward and writes another wakeup byte.
        for identifier in self._channel.take_ready():
            pending = self._pending.get(identifier)
            if pending is not None and not pending[1].done():
                pending[1].set_result(None)

    def _retire(self) -> None:
        if self._retired:
            return
        self._retired = True
        self._discard_cached()
        self._channel.close()
        self._poll_joined()

    def _poll_joined(self) -> None:
        self._poll_handle = None
        if self._closed:
            return
        reader_retired = self._retire_reader()
        waiting = False
        for call, future in tuple(self._pending.values()):
            if call.is_joined():
                if not future.done():
                    future.set_result(None)
            else:
                waiting = True
        loop = self._loop()
        if (waiting or not reader_retired) and loop is not None:
            # Only failed notification channels use this bounded-rate recovery.
            # TimerHandles survive cancellation of every asyncio Task.
            self._poll_handle = loop.call_later(
                0.001, self._poll_joined, context=contextvars.Context()
            )

    def register(self, call: Any) -> tuple[int, asyncio.Future[None]] | None:
        loop = self._loop()
        if loop is None or self._closed or self._retired:
            return None
        self._next_id += 1
        identifier = self._next_id
        joined = loop.create_future()
        self._pending[identifier] = (call, joined)
        try:
            # Register before native work can finish or fail its notification.
            if not call.start_shared(self._channel, identifier):
                self._pending.pop(identifier)
                self._retire()
                return None
        except BaseException:
            self._pending.pop(identifier, None)
            raise
        return identifier, joined

    async def wait(self, call: Any, identifier: int, joined: asyncio.Future[None]) -> Any:
        loop = self._loop()
        assert loop is not None
        try:
            cancelled = None
            while True:
                try:
                    await joined
                    break
                except asyncio.CancelledError as error:
                    cancelled = error
                    call.cancel()
                    if joined.cancelled():
                        joined = loop.create_future()
                        self._pending[identifier] = (call, joined)
                        # If a cancelled Future consumed the wakeup, completion
                        # is already durable. Otherwise publication is still due.
                        if call.is_joined():
                            joined.set_result(None)
            if cancelled is not None:
                call.discard_result()
                raise cancelled
            return call.take_result()
        finally:
            self._pending.pop(identifier, None)

    def close(self) -> None:
        self._closed = True
        try:
            self._discard_cached()
        finally:
            if self._poll_handle is not None:
                self._poll_handle.cancel()
                self._poll_handle = None
            if self._remove_reader():
                if self._channel is not None:
                    self._channel.close()
                if self._reader is not None:
                    self._reader.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def _completion_dispatcher(
    loop: asyncio.AbstractEventLoop, call: Any
) -> _CompletionDispatcher | None:
    try:
        reference = _dispatchers.get(loop)
    except TypeError:
        return None
    dispatcher = reference() if reference is not None else None
    if dispatcher is None or dispatcher._closed or dispatcher._retired:
        try:
            dispatcher = _CompletionDispatcher(loop, call)
        except NotImplementedError:
            return None
        try:
            _dispatchers[loop] = weakref.ref(dispatcher)
        except BaseException:
            try:
                dispatcher.close()
            except BaseException:
                pass
            raise
    return dispatcher


async def _windows_completion_pair() -> tuple[socket.socket, socket.socket]:
    """Create connected sockets without blocking Windows' event-loop thread."""
    loop = asyncio.get_running_loop()
    retrying = False
    while True:
        if retrying:
            # Both failed sockets have closed; allow cancellation before replacement.
            await asyncio.sleep(0)
        with socket.socket() as listener, ExitStack() as cleanup:
            listener.setblocking(False)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            writer = cleanup.enter_context(socket.socket())
            writer.setblocking(False)
            address = listener.getsockname()
            try:
                await loop.sock_connect(writer, address)
            except OSError as error:
                if retrying or getattr(error, "winerror", None) != 52:
                    raise
                retrying = True
                continue
            reader, _ = await loop.sock_accept(listener)
            cleanup.enter_context(reader)
            # Only the reader closes abortively, after receiving the native EOF.
            # This releases the TCP addresses without retaining TIME_WAIT sockets.
            reader.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("HH" if os.name == "nt" else "ii", 1, 0),
            )
            if (
                reader.getsockname() != writer.getpeername()
                or writer.getsockname() != reader.getpeername()
            ):
                raise ConnectionError("Unexpected native completion peer")
            cleanup.pop_all()
            return reader, writer


async def _await_native_completion(call: Any) -> Any:
    """Convert a result only after every real native completion owner retires."""
    try:
        if sys.platform == "linux" and hasattr(call, "start_shared"):
            loop = asyncio.get_running_loop()
            while True:
                dispatcher = _completion_dispatcher(loop, call)
                if dispatcher is None:
                    break
                registered = dispatcher.register(call)
                if registered is not None:
                    return await dispatcher.wait(call, *registered)
        return await _await_socket_completion(call)
    finally:
        call.discard_unstarted()


async def _await_socket_completion(call: Any) -> Any:
    """Join native work through EOF, then convert its owned result in Python."""
    try:
        reader, writer = (
            await _windows_completion_pair() if sys.platform == "win32" else _unix_completion_pair()
        )
        try:
            # start takes the writer and starts one operation. No result publisher
            # calls back into Python; EOF also joins any retained writer operations.
            call.start(writer)
            loop = asyncio.get_running_loop()
            cancelled = None
            while True:
                try:
                    acknowledgement = await loop.sock_recv(reader, 1)
                    break
                except asyncio.CancelledError as error:
                    cancelled = error
                    call.cancel()
                    # EOF persists through repeated cancellation of sock_recv.
            if acknowledgement != b"":
                raise _NativeCompletionError("Native completion acknowledgement was lost")
            if cancelled is None:
                try:
                    # An already-readable EOF may not deliver pending cancellation.
                    await asyncio.sleep(0)
                except asyncio.CancelledError as error:
                    cancelled = error
            if cancelled is not None:
                call.discard_result()
                raise cancelled
            return call.take_result()
        finally:
            writer.close()
            reader.close()
    finally:
        # Setup can be cancelled before start transfers the owned native future.
        # A saved traceback must not retain that future until Python collection.
        call.discard_unstarted()


async def _await_native_event(read: Any) -> Any:
    """Try exactly one managed read, preserving normal completion if it waits."""
    # Even inactive/child events must let close and other Python work run.
    try:
        await asyncio.sleep(0)
        result = read.poll()
    finally:
        # A saved pre-poll cancellation traceback must not keep the native
        # ThreadState owned by this unstarted read.
        read = None
    if isinstance(result, str):
        return result
    try:
        return await _await_native_completion(result)
    finally:
        # After start the usual EOF path owns and joins the read. Before start,
        # release it now instead of retaining locks in an exception traceback.
        result.discard_unstarted()


_preserve_module_identity(globals(), "agents._native_async")
