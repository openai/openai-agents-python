"""Serve the Codex hook callback on a private, run-scoped loopback MCP endpoint."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

import anyio
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from ..mcp._compat import MCP_V2


class _RelayIO:
    """Adapt the local byte stream to the text interface used by MCP stdio."""

    def __init__(self, stream: SocketStream, incoming: BufferedByteReceiveStream):
        self.stream = stream
        self.incoming = incoming

    async def __aiter__(self) -> AsyncIterator[str]:
        line = bytearray()
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            async for chunk in self.incoming:
                first, *rest = chunk.split(b"\n")
                line.extend(first)
                for next_line in rest:
                    yield line.decode("utf-8", errors="replace")
                    line = bytearray(next_line)

    async def write(self, data: str) -> None:
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await self.stream.send(data.encode())

    async def flush(self) -> None:
        # Each write goes directly to the socket.
        pass


@asynccontextmanager
async def serve_hook(
    key: str, dispatch: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
) -> AsyncIterator[int]:
    credential = key.encode("ascii")

    async def list_tools(context: Any, params: Any) -> types.ListToolsResult:
        # Only the configured Codex hook can dispatch here. The model sees no tools.
        return types.ListToolsResult(tools=[])

    async def call_tool(context: Any, params: Any) -> types.CallToolResult:
        arguments = dict(params.arguments or {})
        credential = arguments.pop("key", None)
        if (
            params.name != "dispatch"
            or not isinstance(credential, str)
            or not hmac.compare_digest(credential.encode(), key.encode())
        ):
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Unknown hook invocation.")],
                isError=True,
            )
        decision = await dispatch(arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(decision, allow_nan=False))]
        )

    if MCP_V2:
        server = cast(Any, Server)(
            "Agents SDK native tool hooks", on_list_tools=list_tools, on_call_tool=call_tool
        )
    else:
        server = Server("Agents SDK native tool hooks")

        async def list_request(request: Any) -> Any:
            return types.ServerResult(await list_tools(None, request.params))

        async def call_request(request: Any) -> Any:
            return types.ServerResult(await call_tool(None, request.params))

        server.request_handlers[types.ListToolsRequest] = list_request
        server.request_handlers[types.CallToolRequest] = call_request

    async def handle(stream: SocketStream) -> None:
        async with stream:
            incoming = BufferedByteReceiveStream(stream)
            try:
                presented = await incoming.receive_exactly(len(credential))
            except (anyio.IncompleteRead, anyio.BrokenResourceError):
                return
            if hmac.compare_digest(presented, credential):
                relay = cast(anyio.AsyncFile[str], _RelayIO(stream, incoming))
                async with stdio_server(stdin=relay, stdout=relay) as (read, write):
                    await server.run(read, write, server.create_initialization_options())

    async with await anyio.create_tcp_listener(local_host="127.0.0.1") as listener:
        serve = asyncio.create_task(listener.serve(handle))
        try:
            yield listener.extra(SocketAttribute.local_port)
        finally:
            serve.cancel()
            with suppress(asyncio.CancelledError):
                await serve
