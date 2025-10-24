import contextlib

import anyio
import pytest
from mcp.client.session import MessageHandlerFnT
from mcp.shared.message import SessionMessage
from mcp.shared.session import RequestResponder
from mcp.types import (
    ClientResult,
    Implementation,
    InitializeResult,
    ServerCapabilities,
    ServerNotification,
    ServerRequest,
)

from agents.mcp.server import (
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
    _MCPServerWithClientSession,
)

HandlerMessage = RequestResponder[ServerRequest, ClientResult] | ServerNotification | Exception


class _StubClientSession:
    """Stub ClientSession that records the configured message handler."""

    def __init__(
        self,
        read_stream,
        write_stream,
        read_timeout_seconds,
        *,
        message_handler=None,
        **_: object,
    ) -> None:
        self.message_handler = message_handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def initialize(self) -> InitializeResult:
        capabilities = ServerCapabilities.model_construct()
        server_info = Implementation.model_construct(name="stub", version="1.0")
        return InitializeResult(
            protocolVersion="2024-11-05",
            capabilities=capabilities,
            serverInfo=server_info,
        )


class _MessageHandlerTestServer(_MCPServerWithClientSession):
    def __init__(self, handler: MessageHandlerFnT | None):
        super().__init__(
            cache_tools_list=False,
            client_session_timeout_seconds=None,
            message_handler=handler,
        )

    def create_streams(self):
        @contextlib.asynccontextmanager
        async def _streams():
            send_stream, recv_stream = anyio.create_memory_object_stream[
                SessionMessage | Exception
            ](1)
            try:
                yield recv_stream, send_stream, None
            finally:
                await recv_stream.aclose()
                await send_stream.aclose()

        return _streams()

    @property
    def name(self) -> str:
        return "test-server"


@pytest.mark.asyncio
async def test_client_session_receives_message_handler(monkeypatch):
    captured: dict[str, object] = {}

    def _recording_client_session(*args, **kwargs):
        session = _StubClientSession(*args, **kwargs)
        captured["message_handler"] = session.message_handler
        return session

    monkeypatch.setattr("agents.mcp.server.ClientSession", _recording_client_session)

    class _AsyncHandler:
        async def __call__(self, message: HandlerMessage) -> None:
            del message

    handler: MessageHandlerFnT = _AsyncHandler()

    server = _MessageHandlerTestServer(handler)

    try:
        await server.connect()
    finally:
        await server.cleanup()

    assert captured["message_handler"] is handler


@pytest.mark.parametrize(
    "server_cls, params",
    [
        (MCPServerSse, {"url": "https://example.com"}),
        (MCPServerStreamableHttp, {"url": "https://example.com"}),
        (MCPServerStdio, {"command": "python"}),
    ],
)
def test_message_handler_propagates_to_server_base(server_cls, params):
    class _AsyncHandler:
        async def __call__(self, message: HandlerMessage) -> None:
            del message

    handler: MessageHandlerFnT = _AsyncHandler()

    server = server_cls(params, message_handler=handler)

    assert server.message_handler is handler
