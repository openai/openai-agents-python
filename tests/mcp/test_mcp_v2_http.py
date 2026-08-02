from __future__ import annotations

import asyncio
import json
import socket

import httpx
import mcp
import pytest
import uvicorn
from mcp.server import Server
from mcp.types import ListToolsResult, Tool

from agents.exceptions import UserError
from agents.mcp import MCPServerStreamableHttp
from agents.mcp._compat import MCP_V2, create_v2_client
from agents.mcp.server import (
    _configure_v2_session_id_hook,
    _create_default_streamable_http_client,
    _validated_v2_http_client_factory,
)

pytestmark = pytest.mark.skipif(not MCP_V2, reason="MCP v2 HTTP behavior")
httpx2 = pytest.importorskip("httpx2")


@pytest.mark.asyncio
async def test_v2_streamable_http_negotiates_modern_protocol():
    async def list_tools(_context, _params) -> ListToolsResult:
        return ListToolsResult(
            tools=[Tool(name="probe", input_schema={"type": "object", "properties": {}})]
        )

    app = Server("probe-server", on_list_tools=list_tools).streamable_http_app()
    socket_ = socket.socket()
    socket_.bind(("127.0.0.1", 0))
    socket_.listen()
    port = socket_.getsockname()[1]
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="on", ws="none")
    )
    server_task = asyncio.create_task(uvicorn_server.serve(sockets=[socket_]))

    async def wait_until_started() -> None:
        while not uvicorn_server.started:
            if server_task.done():
                await server_task
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_until_started(), timeout=5)
        server = MCPServerStreamableHttp(params={"url": f"http://127.0.0.1:{port}/mcp"})
        async with server:
            tools = await server.list_tools()
            protocol_version = server.session.protocol_version if server.session else None
            session_id = server.session_id

        assert [tool.name for tool in tools] == ["probe"]
        assert protocol_version == "2026-07-28"
        assert session_id is None
    finally:
        uvicorn_server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_v2_response_hook_only_captures_legacy_initialize_session():
    captured: list[str] = []

    def handle_request(request):
        return httpx2.Response(
            int(request.headers.get("x-response-status", "200")),
            headers={"mcp-session-id": "legacy-session"},
            request=request,
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle_request))
    _configure_v2_session_id_hook(
        client,
        on_session_id=captured.append,
    )

    await client.post(
        "https://example.test/mcp",
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}),
    )
    assert captured == []

    await client.post(
        "https://example.test/mcp",
        headers={"x-response-status": "503"},
        content=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize"}),
    )
    assert captured == []

    await client.post(
        "https://example.test/mcp",
        content=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "initialize"}),
    )
    assert captured == ["legacy-session"]
    await client.aclose()


def test_v2_rejects_initialized_notification_tolerance_before_connecting():
    server = MCPServerStreamableHttp(
        params={
            "url": "https://example.test/mcp",
            "ignore_initialized_notification_failure": True,
        }
    )

    with pytest.raises(UserError, match="not supported with MCP Python SDK v2"):
        server.create_streams()


def test_v2_rejects_v1_auth_before_request():
    with pytest.raises(UserError, match="httpx2.Auth"):
        _create_default_streamable_http_client(auth=httpx.BasicAuth("user", "pass"))


def test_v2_rejects_v1_client_factory_result():
    factory = _validated_v2_http_client_factory(lambda **kwargs: httpx.AsyncClient())
    with pytest.raises(UserError, match="httpx2.AsyncClient"):
        factory()


def test_v2_default_factory_returns_httpx2_client():
    client = _create_default_streamable_http_client()
    assert isinstance(client, httpx2.AsyncClient)


def test_v2_client_receives_timeout_message_handler_and_disables_cache(monkeypatch):
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, transport, **kwargs):
            captured["transport"] = transport
            captured.update(kwargs)

    monkeypatch.setattr(mcp, "Client", StubClient)
    transport = object()
    handler = object()

    create_v2_client(
        transport,
        read_timeout_seconds=12.5,
        message_handler=handler,
    )

    assert captured == {
        "transport": transport,
        "mode": "auto",
        "cache": None,
        "read_timeout_seconds": 12.5,
        "message_handler": handler,
    }
