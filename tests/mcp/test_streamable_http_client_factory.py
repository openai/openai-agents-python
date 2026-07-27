"""Tests for MCPServerStreamableHttp with MCP Python SDK v2 transport."""

from __future__ import annotations

from datetime import timedelta

import httpx2
import pytest

from agents.exceptions import UserError
from agents.mcp import MCPServerStreamableHttp


class TestMCPServerStreamableHttpClientFactory:
    """Tests for MCPServerStreamableHttp behaviour under MCP SDK v2."""

    def test_default_create_streams_returns_context_manager(self):
        """create_streams() returns an async context manager without network calls."""
        server = MCPServerStreamableHttp(
            params={
                "url": "http://localhost:8000/mcp",
                "headers": {"Authorization": "Bearer token"},
                "timeout": 10,
            }
        )
        cm = server.create_streams()
        assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")

    def test_custom_httpx_client_factory_raises_user_error(self):
        """httpx_client_factory is unsupported in mcp SDK v2 and raises UserError."""

        def _factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient()  # pragma: no cover

        server = MCPServerStreamableHttp(
            params={
                "url": "http://localhost:8000/mcp",
                "httpx_client_factory": _factory,
            }
        )
        with pytest.raises(UserError, match="httpx_client_factory is not supported"):
            server.create_streams()

    def test_ignore_initialized_notification_failure_raises_user_error(self):
        """ignore_initialized_notification_failure is unsupported in mcp SDK v2."""
        server = MCPServerStreamableHttp(
            params={
                "url": "http://localhost:8000/mcp",
                "ignore_initialized_notification_failure": True,
            }
        )
        with pytest.raises(UserError, match="ignore_initialized_notification_failure"):
            server.create_streams()

    def test_timedelta_timeout_converted_to_seconds(self):
        """timedelta values for timeout/sse_read_timeout are converted to float."""
        server = MCPServerStreamableHttp(
            params={
                "url": "http://localhost:8000/mcp",
                "timeout": timedelta(seconds=30),
                "sse_read_timeout": timedelta(minutes=5),
            }
        )
        cm = server.create_streams()
        assert hasattr(cm, "__aenter__")

    def test_httpx_client_factory_removed_from_streamable_http_params(self):
        """httpx_client_factory is removed from MCPServerStreamableHttpParams in mcp SDK v2."""
        from agents.mcp.server import MCPServerStreamableHttpParams

        assert hasattr(MCPServerStreamableHttpParams, "__annotations__")
        assert "httpx_client_factory" not in MCPServerStreamableHttpParams.__annotations__
