"""Tests for MCPServerStreamableHttp httpx_client_factory functionality."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.mcp import MCPServerStreamableHttp
from agents.mcp.server import (
    _InitializedNotificationTolerantTransport,
    _wrap_httpx_client_factory_for_initialized_notification_tolerance,
)


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
async def test_initialized_notification_failure_returns_synthetic_success():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.content == b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}':
            return httpx.Response(503, request=request)
        return httpx.Response(200, request=request)

    transport = _InitializedNotificationTolerantTransport(httpx.MockTransport(handler))

    initialized_request = httpx.Request(
        "POST",
        "https://example.test/mcp",
        content=b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}',
    )
    normal_request = httpx.Request(
        "POST",
        "https://example.test/mcp",
        content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
    )

    initialized_response = await transport.handle_async_request(initialized_request)
    normal_response = await transport.handle_async_request(normal_request)

    assert initialized_response.status_code == 202
    assert normal_response.status_code == 200


@pytest.mark.asyncio
async def test_initialized_notification_transport_exception_returns_synthetic_success():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.content == b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}':
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, request=request)

    transport = _InitializedNotificationTolerantTransport(httpx.MockTransport(handler))
    request = httpx.Request(
        "POST",
        "https://example.test/mcp",
        content=b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}',
    )

    response = await transport.handle_async_request(request)

    assert response.status_code == 202


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

        kwargs = mock_client.call_args.kwargs
        assert kwargs["url"] == "http://localhost:8000/mcp"
        assert kwargs["headers"] is None
        assert kwargs["timeout"] == 5
        assert kwargs["sse_read_timeout"] == 300
        assert kwargs["terminate_on_close"] is True

        factory = kwargs["httpx_client_factory"]
        client = factory()
        try:
            assert isinstance(client._transport, _InitializedNotificationTolerantTransport)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_factory_wrapper_preserves_non_initialized_failures():
    def base_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        del headers, timeout, auth

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    wrapped_factory = _wrap_httpx_client_factory_for_initialized_notification_tolerance(
        base_factory
    )
    client = wrapped_factory()
    try:
        with pytest.raises(httpx.ConnectError):
            await client.post(
                "https://example.test/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
    finally:
        await client.aclose()
