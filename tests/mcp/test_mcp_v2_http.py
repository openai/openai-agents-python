from __future__ import annotations

import asyncio
import json
import socket
import traceback
from typing import Any

import httpx
import mcp
import pytest
import uvicorn
from mcp.server import Server
from mcp.types import ListToolsResult, TextContent, Tool

from agents.exceptions import UserError
from agents.mcp import MCPServerStreamableHttp
from agents.mcp._compat import MCP_V2, MCPError, create_v2_client
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

    with pytest.raises(httpx2.HTTPStatusError):
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


@pytest.mark.asyncio
async def test_v2_response_hook_raises_5xx_for_a_request_that_is_not_a_transport_message():
    def handle_request(request):
        return httpx2.Response(503, request=request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle_request))
    _configure_v2_session_id_hook(client, on_session_id=None)

    # An OAuth dynamic client registration body is JSON, but it is not a transport message, so
    # its failures stay on the HTTP error path instead of MCP's body-bearing OAuth exceptions.
    with pytest.raises(httpx2.HTTPStatusError):
        await client.post(
            "https://example.test/register",
            content=json.dumps(
                {
                    "client_name": "example",
                    "redirect_uris": ["https://example.test/callback"],
                }
            ),
        )

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


def _v2_response_for_request(
    request,
    *,
    fail_tool_call: bool = False,
    tool_status_code: int | None = None,
):
    payload = json.loads(request.content) if request.content else {}
    method = payload.get("method")
    if method == "server/discover":
        body = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "error": {"code": -32601, "message": "Method not found"},
        }
    elif method == "initialize":
        body = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            },
        }
    elif method == "notifications/initialized":
        return httpx2.Response(202, request=request)
    elif method == "tools/list":
        body = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "tools": [
                    {
                        "name": "test",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
        }
    elif method == "tools/call" and tool_status_code is not None:
        return httpx2.Response(tool_status_code, request=request)
    elif method == "tools/call" and fail_tool_call:
        raise httpx2.ConnectError("connection dropped", request=request)
    elif method == "tools/call":
        body = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False,
            },
        }
    else:
        body = {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32601, "message": "Unknown method"},
        }
    return httpx2.Response(
        200,
        json=body,
        headers={"content-type": "application/json"},
        request=request,
    )


@pytest.mark.asyncio
async def test_v2_streamable_http_retries_connect_error_on_isolated_session():
    clients: list[Any] = []

    def factory(headers=None, timeout=None, auth=None):
        fail_tool_call = not clients

        async def handler(request):
            return _v2_response_for_request(request, fail_tool_call=fail_tool_call)

        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )
        clients.append(client)
        return client

    server = MCPServerStreamableHttp(
        params={
            "url": "https://example.test/mcp",
            "httpx_client_factory": factory,
        },
        max_retry_attempts=1,
        retry_backoff_seconds_base=0,
    )

    async with server:
        result = await asyncio.wait_for(server.call_tool("test", {}), timeout=2)

    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "ok"
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)


def _first_tool_call_returns_503_factory(clients: list[Any], tool_call_statuses: list[int]):
    """Build clients whose server answers only the first `tools/call` with HTTP 503."""

    def factory(headers=None, timeout=None, auth=None):
        async def handler(request):
            payload = json.loads(request.content) if request.content else {}
            if payload.get("method") == "tools/call":
                status_code = 503 if not tool_call_statuses else 200
                tool_call_statuses.append(status_code)
                if status_code == 503:
                    return _v2_response_for_request(request, tool_status_code=503)
            return _v2_response_for_request(request)

        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )
        clients.append(client)
        return client

    return factory


@pytest.mark.asyncio
async def test_v2_streamable_http_5xx_fails_only_that_request():
    clients: list[Any] = []
    tool_call_statuses: list[int] = []
    server = MCPServerStreamableHttp(
        params={
            "url": "https://example.test/mcp",
            "httpx_client_factory": _first_tool_call_returns_503_factory(
                clients, tool_call_statuses
            ),
        },
    )

    async with server:
        with pytest.raises(MCPError):
            await asyncio.wait_for(server.call_tool("test", {}), timeout=2)
        result = await asyncio.wait_for(server.call_tool("test", {}), timeout=2)
        tools = await asyncio.wait_for(server.list_tools(), timeout=2)

    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "ok"
    assert [tool.name for tool in tools] == ["test"]
    assert tool_call_statuses == [503, 200]
    assert len(clients) == 1
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_v2_streamable_http_retries_5xx_on_shared_session():
    clients: list[Any] = []
    tool_call_statuses: list[int] = []
    server = MCPServerStreamableHttp(
        params={
            "url": "https://example.test/mcp",
            "httpx_client_factory": _first_tool_call_returns_503_factory(
                clients, tool_call_statuses
            ),
        },
        max_retry_attempts=1,
        retry_backoff_seconds_base=0,
    )

    async with server:
        result = await asyncio.wait_for(server.call_tool("test", {}), timeout=2)

    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "ok"
    assert tool_call_statuses == [503, 200]
    assert len(clients) == 1
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_v2_streamable_http_initialized_notification_5xx_keeps_session_usable():
    clients: list[Any] = []

    def factory(headers=None, timeout=None, auth=None):
        async def handler(request):
            payload = json.loads(request.content) if request.content else {}
            if payload.get("method") == "notifications/initialized":
                return httpx2.Response(503, request=request)
            return _v2_response_for_request(request)

        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )
        clients.append(client)
        return client

    server = MCPServerStreamableHttp(
        params={
            "url": "https://example.test/mcp",
            "httpx_client_factory": factory,
        },
    )

    async with server:
        result = await asyncio.wait_for(server.call_tool("test", {}), timeout=2)
        tools = await asyncio.wait_for(server.list_tools(), timeout=2)

    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "ok"
    assert [tool.name for tool in tools] == ["test"]
    assert len(clients) == 1


@pytest.mark.asyncio
async def test_v2_streamable_http_handshake_5xx_fails_connect_without_legacy_fallback():
    methods: list[str | None] = []

    def factory(headers=None, timeout=None, auth=None):
        async def handler(request):
            payload = json.loads(request.content) if request.content else {}
            methods.append(payload.get("method"))
            if payload.get("method") == "server/discover":
                return httpx2.Response(503, request=request)
            return _v2_response_for_request(request)

        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    server = MCPServerStreamableHttp(
        params={
            "url": "https://example.test/mcp",
            "httpx_client_factory": factory,
        },
    )

    with pytest.raises(UserError, match="HTTP error 503"):
        await server.connect()

    assert methods == ["server/discover"]
    assert server.session is None


@pytest.mark.asyncio
async def test_v2_streamable_http_oauth_subrequest_5xx_keeps_http_error_mapping():
    from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider

    response_body_marker = "synthetic-authorization-server-body"

    class _UnauthenticatedStorage:
        async def get_tokens(self):
            return None

        async def set_tokens(self, tokens):
            return None

        async def get_client_info(self):
            return None

        async def set_client_info(self, client_information):
            return None

    def factory(headers=None, timeout=None, auth=None):
        async def handler(request):
            path = request.url.path
            if path == "/token":
                return httpx2.Response(503, text=response_body_marker, request=request)
            if path.startswith("/.well-known/oauth-protected-resource"):
                return httpx2.Response(
                    200,
                    json={
                        "resource": "https://example.test/mcp",
                        "authorization_servers": ["https://example.test"],
                    },
                    request=request,
                )
            if path.startswith("/.well-known/oauth-authorization-server"):
                return httpx2.Response(
                    200,
                    json={
                        "issuer": "https://example.test",
                        "authorization_endpoint": "https://example.test/authorize",
                        "token_endpoint": "https://example.test/token",
                        "response_types_supported": ["code"],
                    },
                    request=request,
                )
            if "authorization" not in request.headers:
                return httpx2.Response(
                    401,
                    headers={
                        "www-authenticate": (
                            "Bearer resource_metadata="
                            '"https://example.test/.well-known/oauth-protected-resource/mcp"'
                        )
                    },
                    request=request,
                )
            return _v2_response_for_request(request)

        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    server = MCPServerStreamableHttp(
        params={
            "url": "https://example.test/mcp",
            "auth": ClientCredentialsOAuthProvider(
                server_url="https://example.test/mcp",
                storage=_UnauthenticatedStorage(),
                client_id="placeholder-client-id",
                client_secret="placeholder-client-secret",
            ),
            "httpx_client_factory": factory,
        },
    )

    with pytest.raises(UserError, match="HTTP error 503") as exc_info:
        await server.connect()

    error = exc_info.value
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert response_body_marker not in rendered
    assert server.session is None


@pytest.mark.asyncio
async def test_v2_connect_cancellation_stops_pending_client_owner(monkeypatch):
    client_entered = asyncio.Event()
    owner_task: asyncio.Task[None] | None = None

    class BlockingClient:
        async def __aenter__(self):
            nonlocal owner_task
            owner_task = asyncio.current_task()
            client_entered.set()
            await asyncio.Event().wait()

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(
        "agents.mcp.server.create_v2_client",
        lambda *args, **kwargs: BlockingClient(),
    )
    server = MCPServerStreamableHttp(params={"url": "https://example.test/mcp"})
    connect_task = asyncio.create_task(server.connect())
    await asyncio.wait_for(client_entered.wait(), timeout=2)

    connect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(connect_task, timeout=2)

    assert owner_task is not None
    assert owner_task.done()
    assert server.session is None


@pytest.mark.asyncio
async def test_v2_streamable_http_preserves_outer_cancellation():
    call_started = asyncio.Event()
    clients: list[Any] = []

    def factory(headers=None, timeout=None, auth=None):
        async def handler(request):
            payload = json.loads(request.content) if request.content else {}
            if payload.get("method") == "tools/call":
                call_started.set()
                await asyncio.Event().wait()
            return _v2_response_for_request(request)

        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )
        clients.append(client)
        return client

    server = MCPServerStreamableHttp(
        params={
            "url": "https://example.test/mcp",
            "httpx_client_factory": factory,
        },
        max_retry_attempts=1,
        retry_backoff_seconds_base=0,
    )

    async with server:
        call_task = asyncio.create_task(server.call_tool("test", {}))
        await asyncio.wait_for(call_started.wait(), timeout=2)
        call_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task

    assert len(clients) == 1
    assert clients[0].is_closed
