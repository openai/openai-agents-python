"""Join native completion publishers before releasing Python ownership."""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import sys
from contextlib import ExitStack
from typing import Any, cast


class _NativeCompletionError(RuntimeError):
    """The native operation's publication acknowledgement was not confirmed."""


async def _windows_completion_pair() -> tuple[socket.socket, socket.socket]:
    """Create connected sockets without blocking Windows' event-loop thread."""
    loop = asyncio.get_running_loop()
    with socket.socket() as listener, ExitStack() as cleanup:
        listener.setblocking(False)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        writer = cleanup.enter_context(socket.socket())
        writer.setblocking(False)
        await loop.sock_connect(writer, listener.getsockname())
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
    """Join native work through EOF, then convert its owned result in Python."""
    try:
        reader, writer = (
            await _windows_completion_pair() if sys.platform == "win32" else socket.socketpair()
        )
        reader.setblocking(False)
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


async def _await_native_event(read: Any) -> str:
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
        return cast(str, await _await_native_completion(result))
    finally:
        # After start the usual EOF path owns and joins the read. Before start,
        # release it now instead of retaining locks in an exception traceback.
        result.discard_unstarted()
