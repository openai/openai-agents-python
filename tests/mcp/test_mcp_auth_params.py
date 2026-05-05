"""Tests for auth and httpx_client_factory params on MCPServerSse and MCPServerStreamableHttp."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.mcp import MCPServerSse, MCPServerStreamableHttp


class TestMCPServerSseAuthAndFactory:
    """Tests for auth and httpx_client_factory added to MCPServerSseParams."""

    @pytest.mark.asyncio
    async def test_sse_default_no_auth_no_factory(self):
        """SSE create_streams passes only the four base params when no extras are set."""
        with patch("agents.mcp.server.sse_client") as mock_client:
            mock_client.return_value = MagicMock()
            server = MCPServerSse(params={"url": "http://localhost:8000/sse"})
            server.create_streams()
            mock_client.assert_called_once_with(
                url="http://localhost:8000/sse",
                headers=None,
                timeout=5,
                sse_read_timeout=300,
            )

    @pytest.mark.asyncio
    async def test_sse_with_auth(self):
        """SSE create_streams forwards the auth parameter when provided."""
        auth = httpx.BasicAuth(username="user", password="pass")
        with patch("agents.mcp.server.sse_client") as mock_client:
            mock_client.return_value = MagicMock()
            server = MCPServerSse(params={"url": "http://localhost:8000/sse", "auth": auth})
            server.create_streams()
            mock_client.assert_called_once_with(
                url="http://localhost:8000/sse",
                headers=None,
                timeout=5,
                sse_read_timeout=300,
                auth=auth,
            )

    @pytest.mark.asyncio
    async def test_sse_with_httpx_client_factory(self):
        """SSE create_streams forwards a custom httpx_client_factory when provided."""

        def custom_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(verify=False)  # pragma: no cover

        with patch("agents.mcp.server.sse_client") as mock_client:
            mock_client.return_value = MagicMock()
            server = MCPServerSse(
                params={
                    "url": "http://localhost:8000/sse",
                    "httpx_client_factory": custom_factory,
                }
            )
            server.create_streams()
            mock_client.assert_called_once_with(
                url="http://localhost:8000/sse",
                headers=None,
                timeout=5,
                sse_read_timeout=300,
                httpx_client_factory=custom_factory,
            )

    @pytest.mark.asyncio
    async def test_sse_with_auth_and_factory(self):
        """SSE create_streams forwards both auth and httpx_client_factory together."""
        auth = httpx.BasicAuth(username="user", password="pass")

        def custom_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(verify=False)  # pragma: no cover

        with patch("agents.mcp.server.sse_client") as mock_client:
            mock_client.return_value = MagicMock()
            server = MCPServerSse(
                params={
                    "url": "http://localhost:8000/sse",
                    "headers": {"X-Token": "abc"},
                    "auth": auth,
                    "httpx_client_factory": custom_factory,
                }
            )
            server.create_streams()
            mock_client.assert_called_once_with(
                url="http://localhost:8000/sse",
                headers={"X-Token": "abc"},
                timeout=5,
                sse_read_timeout=300,
                auth=auth,
                httpx_client_factory=custom_factory,
            )


class TestMCPServerStreamableHttpAuth:
    """Tests for the auth parameter added to MCPServerStreamableHttpParams."""

    @pytest.mark.asyncio
    async def test_streamable_http_default_no_auth(self):
        """StreamableHttp create_streams omits auth when not provided."""
        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()
            server = MCPServerStreamableHttp(params={"url": "http://localhost:8000/mcp"})
            server.create_streams()
            mock_client.assert_called_once_with(
                url="http://localhost:8000/mcp",
                headers=None,
                timeout=5,
                sse_read_timeout=300,
                terminate_on_close=True,
            )

    @pytest.mark.asyncio
    async def test_streamable_http_with_auth(self):
        """StreamableHttp create_streams forwards the auth parameter when provided."""
        auth = httpx.BasicAuth(username="user", password="pass")
        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()
            server = MCPServerStreamableHttp(
                params={"url": "http://localhost:8000/mcp", "auth": auth}
            )
            server.create_streams()
            mock_client.assert_called_once_with(
                url="http://localhost:8000/mcp",
                headers=None,
                timeout=5,
                sse_read_timeout=300,
                terminate_on_close=True,
                auth=auth,
            )

    @pytest.mark.asyncio
    async def test_streamable_http_with_auth_and_factory(self):
        """StreamableHttp create_streams forwards both auth and httpx_client_factory."""
        auth = httpx.BasicAuth(username="user", password="pass")

        def custom_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(verify=False)  # pragma: no cover

        with patch("agents.mcp.server.streamablehttp_client") as mock_client:
            mock_client.return_value = MagicMock()
            server = MCPServerStreamableHttp(
                params={
                    "url": "http://localhost:8000/mcp",
                    "auth": auth,
                    "httpx_client_factory": custom_factory,
                }
            )
            server.create_streams()
            mock_client.assert_called_once_with(
                url="http://localhost:8000/mcp",
                headers=None,
                timeout=5,
                sse_read_timeout=300,
                terminate_on_close=True,
                auth=auth,
                httpx_client_factory=custom_factory,
            )
