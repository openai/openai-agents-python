"""Tests for MCPServerStreamableHttp httpx_client_factory functionality."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.mcp import MCPServerStreamableHttp


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
