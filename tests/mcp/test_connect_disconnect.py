from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import ListToolsResult, Tool as MCPTool

from agents.mcp import MCPServerStdio

from .helpers import DummyStreamsContextManager, tee


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_async_ctx_manager_works(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """Test that the async context manager works."""
    server = MCPServerStdio(
        params={
            "command": tee,
        },
        cache_tools_list=True,
    )

    tools = [
        MCPTool(name="tool1", inputSchema={}),
        MCPTool(name="tool2", inputSchema={}),
    ]

    mock_list_tools.return_value = ListToolsResult(tools=tools)

    assert server.session is None, "Server should not be connected"

    async with server:
        assert server.session is not None, "Server should be connected"

    assert server.session is None, "Server should be disconnected"


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_manual_connect_disconnect_works(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """Test that the async context manager works."""
    server = MCPServerStdio(
        params={
            "command": tee,
        },
        cache_tools_list=True,
    )

    tools = [
        MCPTool(name="tool1", inputSchema={}),
        MCPTool(name="tool2", inputSchema={}),
    ]

    mock_list_tools.return_value = ListToolsResult(tools=tools)

    assert server.session is None, "Server should not be connected"

    await server.connect()
    assert server.session is not None, "Server should be connected"

    await server.cleanup()
    assert server.session is None, "Server should be disconnected"
