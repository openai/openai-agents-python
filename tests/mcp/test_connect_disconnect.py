from unittest.mock import AsyncMock, patch

import pytest
from mcp import StdioServerParameters
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


@pytest.mark.asyncio
@patch("agents.mcp.server.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
async def test_stdio_suppresses_resource_tracker_warnings(
    mock_initialize: AsyncMock, mock_stdio_client: AsyncMock
):
    """Test that MCPServerStdio suppresses resource_tracker semaphore warnings."""
    server = MCPServerStdio(
        params={"command": tee},
        cache_tools_list=True,
    )

    async with server:
        pass

    # Verify stdio_client was called with params containing PYTHONWARNINGS.
    mock_stdio_client.assert_called_once()
    params = mock_stdio_client.call_args[0][0]
    assert isinstance(params, StdioServerParameters)
    assert params.env is not None
    assert "PYTHONWARNINGS" in params.env
    assert "resource_tracker" in params.env["PYTHONWARNINGS"]


@pytest.mark.asyncio
@patch("agents.mcp.server.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
async def test_stdio_preserves_existing_pythonwarnings(
    mock_initialize: AsyncMock, mock_stdio_client: AsyncMock
):
    """Test that existing PYTHONWARNINGS values are preserved."""
    server = MCPServerStdio(
        params={
            "command": tee,
            "env": {"PYTHONWARNINGS": "error::DeprecationWarning"},
        },
        cache_tools_list=True,
    )

    async with server:
        pass

    params = mock_stdio_client.call_args[0][0]
    assert params.env is not None
    assert "error::DeprecationWarning" in params.env["PYTHONWARNINGS"]
    assert "resource_tracker" in params.env["PYTHONWARNINGS"]
