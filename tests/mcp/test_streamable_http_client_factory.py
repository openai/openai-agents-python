"""Tests for MCPServerStreamableHttp httpx_client_factory functionality."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import anyio
import httpx
import pytest
from mcp.shared.message import JSONRPCMessage, SessionMessage
from mcp.types import JSONRPCNotification, JSONRPCRequest

from agents.mcp import MCPServerStreamableHttp
from agents.mcp.server import _AgentsStreamableHTTPTransport


class TestMCPServerStreamableHttpClientFactory:
    """Test cases for custom httpx_client_factory parameter."""

    @pytest.mark.asyncio
    async def test_default_httpx_client_factory(self):
        """Test that default behavior works when no custom factory is provided."""
        # Mock the streamablehttp_client to avoid actual network calls
        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()

            server = MCPServerStreamableHttp(
                params={
                    "url": "http://localhost:8000/mcp",
                    "headers": {"Authorization": "Bearer token"},
                    "timeout": 10,
                }
            )

            # Create streams should not pass httpx_client_factory when not provided
            server.create_streams()

            # Verify streamablehttp_client was called with correct parameters
            mock_client.assert_called_once_with(
                url="http://localhost:8000/mcp",
                headers={"Authorization": "Bearer token"},
                timeout=10,
                sse_read_timeout=300,  # Default value
                terminate_on_close=True,  # Default value
                # httpx_client_factory should not be passed when not provided
            )

    @pytest.mark.asyncio
    async def test_custom_httpx_client_factory(self):
        """Test that custom httpx_client_factory is passed correctly."""

        # Create a custom factory function
        def custom_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                verify=False,  # Disable SSL verification for testing
                timeout=httpx.Timeout(60.0),
                headers={"X-Custom-Header": "test"},
            )

        # Mock the streamablehttp_client to avoid actual network calls
        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()

            server = MCPServerStreamableHttp(
                params={
                    "url": "http://localhost:8000/mcp",
                    "headers": {"Authorization": "Bearer token"},
                    "timeout": 10,
                    "httpx_client_factory": custom_factory,
                }
            )

            # Create streams should pass the custom factory
            server.create_streams()

            # Verify streamablehttp_client was called with the custom factory
            mock_client.assert_called_once_with(
                url="http://localhost:8000/mcp",
                headers={"Authorization": "Bearer token"},
                timeout=10,
                sse_read_timeout=300,  # Default value
                terminate_on_close=True,  # Default value
                httpx_client_factory=custom_factory,
            )

    @pytest.mark.asyncio
    async def test_custom_httpx_client_factory_with_ssl_cert(self):
        """Test custom factory with SSL certificate configuration."""

        def ssl_cert_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                verify="/path/to/cert.pem",  # Custom SSL certificate
                timeout=httpx.Timeout(120.0),
            )

        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()

            server = MCPServerStreamableHttp(
                params={
                    "url": "https://secure-server.com/mcp",
                    "timeout": 30,
                    "httpx_client_factory": ssl_cert_factory,
                }
            )

            server.create_streams()

            mock_client.assert_called_once_with(
                url="https://secure-server.com/mcp",
                headers=None,
                timeout=30,
                sse_read_timeout=300,
                terminate_on_close=True,
                httpx_client_factory=ssl_cert_factory,
            )

    @pytest.mark.asyncio
    async def test_custom_httpx_client_factory_with_proxy(self):
        """Test custom factory with proxy configuration."""

        def proxy_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                proxy="http://proxy.example.com:8080",
                timeout=httpx.Timeout(60.0),
            )

        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()

            server = MCPServerStreamableHttp(
                params={
                    "url": "http://localhost:8000/mcp",
                    "httpx_client_factory": proxy_factory,
                }
            )

            server.create_streams()

            mock_client.assert_called_once_with(
                url="http://localhost:8000/mcp",
                headers=None,
                timeout=5,  # Default value
                sse_read_timeout=300,
                terminate_on_close=True,
                httpx_client_factory=proxy_factory,
            )

    @pytest.mark.asyncio
    async def test_custom_httpx_client_factory_with_retry_logic(self):
        """Test custom factory with retry logic configuration."""

        def retry_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                # Note: httpx doesn't have built-in retry, but this shows how
                # a custom factory could be used to configure retry behavior
                # through middleware or other mechanisms
            )

        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()

            server = MCPServerStreamableHttp(
                params={
                    "url": "http://localhost:8000/mcp",
                    "httpx_client_factory": retry_factory,
                }
            )

            server.create_streams()

            mock_client.assert_called_once_with(
                url="http://localhost:8000/mcp",
                headers=None,
                timeout=5,
                sse_read_timeout=300,
                terminate_on_close=True,
                httpx_client_factory=retry_factory,
            )

    def test_httpx_client_factory_type_annotation(self):
        """Test that the type annotation is correct for httpx_client_factory."""
        from agents.mcp.server import MCPServerStreamableHttpParams

        # This test ensures the type annotation is properly set
        # We can't easily test the TypedDict at runtime, but we can verify
        # that the import works and the type is available
        assert hasattr(MCPServerStreamableHttpParams, "__annotations__")

        # Verify that the httpx_client_factory parameter is in the annotations
        annotations = MCPServerStreamableHttpParams.__annotations__
        assert "httpx_client_factory" in annotations

        # The annotation should contain the string representation of the type
        annotation_str = str(annotations["httpx_client_factory"])
        assert "HttpClientFactory" in annotation_str

    @pytest.mark.asyncio
    async def test_all_parameters_with_custom_factory(self):
        """Test that all parameters work together with custom factory."""

        def comprehensive_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                verify=False,
                timeout=httpx.Timeout(90.0),
                headers={"X-Test": "value"},
            )

        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()

            server = MCPServerStreamableHttp(
                params={
                    "url": "https://api.example.com/mcp",
                    "headers": {"Authorization": "Bearer token"},
                    "timeout": 45,
                    "sse_read_timeout": 600,
                    "terminate_on_close": False,
                    "httpx_client_factory": comprehensive_factory,
                }
            )

            server.create_streams()

            mock_client.assert_called_once_with(
                url="https://api.example.com/mcp",
                headers={"Authorization": "Bearer token"},
                timeout=45,
                sse_read_timeout=600,
                terminate_on_close=False,
                httpx_client_factory=comprehensive_factory,
            )


@pytest.mark.asyncio
async def test_initialized_notification_failure_does_not_stop_following_requests():
    transport = _AgentsStreamableHTTPTransport(
        "https://example.test/mcp",
        ignore_initialized_notification_failure=True,
    )
    request_handled = asyncio.Event()

    async def fake_handle_post_request(ctx):
        message = ctx.session_message.message
        if transport._is_initialized_notification(message):
            request = httpx.Request("POST", "https://example.test/mcp")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("HTTP error 503", request=request, response=response)
        request_handled.set()

    transport._handle_post_request = fake_handle_post_request  # type: ignore[method-assign]

    read_stream_writer, _ = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    initialized_notification = SessionMessage(
        JSONRPCMessage(
            JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized", params={})
        )
    )
    list_tools_request = SessionMessage(
        JSONRPCMessage(JSONRPCRequest(jsonrpc="2.0", id=1, method="tools/list", params={}))
    )

    async with httpx.AsyncClient() as client:
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                transport.post_writer,
                client,
                write_stream_reader,
                read_stream_writer,
                write_stream,
                lambda: None,
                tg,
            )
            await write_stream.send(initialized_notification)
            await write_stream.send(list_tools_request)

            await asyncio.wait_for(request_handled.wait(), timeout=1)

            await write_stream.aclose()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_streamable_http_server_passes_ignore_initialized_notification_failure():
    with patch("agents.mcp.server.streamablehttp_client") as mock_client:
        mock_client.return_value = MagicMock()

        server = MCPServerStreamableHttp(
            params={
                "url": "http://localhost:8000/mcp",
                "ignore_initialized_notification_failure": True,
            }
        )

        server.create_streams()

        mock_client.assert_called_once_with(
            url="http://localhost:8000/mcp",
            headers=None,
            timeout=5,
            sse_read_timeout=300,
            terminate_on_close=True,
            ignore_initialized_notification_failure=True,
        )
