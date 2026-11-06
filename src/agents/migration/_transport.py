"""Private one-use Responses byte carrier; application callbacks run elsewhere."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

_CARRIER_MODEL = "gpt-5.5"


class _ResponseCarrier:
    def __init__(self) -> None:
        self.key = secrets.token_urlsafe(32)
        self.url = ""
        self.active = True
        self.pending: bytes | None = None
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=16384)
        assert self._server.sockets is not None
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/v1"

    def retire(self) -> None:
        self.active = False
        self.pending = None
        if self._server is not None:
            self._server.close()

    async def close(self) -> None:
        self.retire()
        tasks = tuple(self._tasks)
        writers = tuple(self._writers)
        for writer in writers:
            # Retirement revokes delivery too. Discard buffered bytes rather
            # than waiting for a stalled peer to receive a completed response.
            writer.transport.abort()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers), return_exceptions=True
            )
        self._writers.clear()
        if self._server is not None:
            await self._server.wait_closed()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.active:
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = header.decode("ascii").split("\r\n")
            fields: dict[str, str] = {}
            for line in lines[1:-2]:
                name, value = line.split(":", 1)
                name = name.lower()
                if name in fields:
                    raise ValueError("Duplicate header")
                fields[name] = value.strip()
            length = int(fields.get("content-length", "0"))
            if (
                lines[0] != "POST /v1/responses HTTP/1.1"
                or not secrets.compare_digest(fields.get("authorization", ""), f"Bearer {self.key}")
                or "transfer-encoding" in fields
                or fields.get("content-encoding", "identity") != "identity"
                or length <= 0
            ):
                raise ValueError("Invalid carrier request")
            if length > 4 * 1024 * 1024:
                writer.write(
                    b"HTTP/1.1 413 Content Too Large\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await asyncio.wait_for(writer.drain(), 10)
                return
            body = json.loads(await asyncio.wait_for(reader.readexactly(length), 10))
            # Authentication, state check and consumption have no intervening await.
            if (
                not self.active
                or self.pending is None
                or not isinstance(body, dict)
                or body.get("model") != _CARRIER_MODEL
            ):
                raise ValueError("No admitted response")
            payload, self.pending = self.pending, None
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await asyncio.wait_for(writer.drain(), 10)
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self._writers.discard(writer)
