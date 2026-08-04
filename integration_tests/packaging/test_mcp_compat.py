from __future__ import annotations

import importlib.metadata
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp

pytestmark = pytest.mark.mcp_compat

LEGACY_SERVER_PATH = Path(__file__).with_name("mcp_legacy_server.py")


@pytest.mark.asyncio
async def test_packaged_client_supports_mcp_v1() -> None:
    expected_version = os.environ["OPENAI_AGENTS_INTEGRATION_MCP_VERSION"]
    assert importlib.metadata.version("mcp") == expected_version

    server = MCPServerStdio(
        name="legacy-test-server",
        params={"command": sys.executable, "args": [str(LEGACY_SERVER_PATH)]},
    )

    async with server:
        tools = await server.list_tools()
        result = await server.call_tool("legacy_tool", {})

    assert [tool.name for tool in tools] == ["legacy_tool"]
    assert getattr(result, "isError", getattr(result, "is_error", None)) is False
    assert result.content[0].type == "text"
    assert result.content[0].text == "legacy-result"


def test_packaged_client_uses_mcp_v1_sse_transport() -> None:
    with patch("agents.mcp.server.sse_client") as mock_client:
        mock_client.return_value = MagicMock()
        server = MCPServerSse(
            params={
                "url": "https://example.test/sse",
                "headers": {"Authorization": "Bearer token"},
            }
        )

        server.create_streams()

    mock_client.assert_called_once()
    assert mock_client.call_args.kwargs["url"] == "https://example.test/sse"
    assert mock_client.call_args.kwargs["headers"] == {"Authorization": "Bearer token"}
    assert mock_client.call_args.kwargs["timeout"] == 5
    assert mock_client.call_args.kwargs["sse_read_timeout"] == 300
    assert callable(mock_client.call_args.kwargs["httpx_client_factory"])


def test_packaged_client_uses_mcp_v1_streamable_http_auth_and_factory() -> None:
    auth = httpx.BasicAuth("user", "pass")

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth)

    with patch("agents.mcp.server.streamablehttp_client") as mock_client:
        mock_client.return_value = MagicMock()
        server = MCPServerStreamableHttp(
            params={
                "url": "https://example.test/mcp",
                "auth": auth,
                "httpx_client_factory": factory,
            }
        )

        server.create_streams()

    mock_client.assert_called_once_with(
        url="https://example.test/mcp",
        headers=None,
        timeout=5,
        sse_read_timeout=300,
        terminate_on_close=True,
        auth=auth,
        httpx_client_factory=factory,
    )
