"""Tests for auth and httpx_client_factory params on MCPServerSse and MCPServerStreamableHttp."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx2
import pytest

from agents.mcp import MCPServerSse, MCPServerStreamableHttp
from agents.mcp.server import _create_default_streamable_http_client


class TestMCPServerSseAuthAndFactory:
    """Tests for auth and httpx_client_factory added to MCPServerSseParams."""

    @pytest.mark.asyncio
    async def test_sse_default_no_auth_no_factory(self):
        """SSE create_streams falls back to the hardened default httpx_client_factory."""
        with patch("agents.mcp.server.sse_client") as mock_client:
            mock_client.return_value = MagicMock()
            server = MCPServerSse(params={"url": "http://localhost:8000/sse"})
            server.create_streams()
            mock_client.assert_called_once_with(
                url="http://localhost:8000/sse",
                headers=None,
                timeout=5,
                sse_read_timeout=300,
                httpx_client_factory=_create_default_streamable_http_client,
            )

    @pytest.mark.asyncio
    async def test_sse_with_auth(self):
        """SSE create_streams forwards auth and still applies the hardened default factory."""
        auth = httpx2.BasicAuth(username="user", password="pass")
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
                httpx_client_factory=_create_default_streamable_http_client,
            )

    @pytest.mark.asyncio
    async def test_sse_with_httpx_client_factory(self):
        """SSE create_streams forwards a custom httpx_client_factory when provided."""

        def custom_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(verify=False)  # pragma: no cover

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
        auth = httpx2.BasicAuth(username="user", password="pass")

        def custom_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(verify=False)  # pragma: no cover

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
    """Tests for MCPServerStreamableHttp behaviour under MCP SDK v2."""

    def test_streamable_http_default_returns_context_manager(self):
        """create_streams() returns an async CM; no old-style httpx_client_factory call."""
        server = MCPServerStreamableHttp(params={"url": "http://localhost:8000/mcp"})
        cm = server.create_streams()
        assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")

    def test_streamable_http_with_auth_returns_context_manager(self):
        """auth is accepted and create_streams() still returns an async CM."""
        import httpx2

        auth = httpx2.BasicAuth(username="user", password="pass")
        server = MCPServerStreamableHttp(params={"url": "http://localhost:8000/mcp", "auth": auth})
        cm = server.create_streams()
        assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")

    def test_streamable_http_with_auth_and_factory_raises_user_error(self):
        """httpx_client_factory is not supported in mcp SDK v2; UserError is raised."""
        from agents.exceptions import UserError

        auth = httpx2.BasicAuth(username="user", password="pass")

        def custom_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(verify=False)  # pragma: no cover

        server = MCPServerStreamableHttp(
            params={
                "url": "http://localhost:8000/mcp",
                "auth": auth,
                "httpx_client_factory": custom_factory,
            }
        )
        with pytest.raises(UserError, match="httpx_client_factory is not supported"):
            server.create_streams()
