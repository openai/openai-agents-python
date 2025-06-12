from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import ListToolsResult, Tool as MCPTool

from agents.mcp import MCPServerStdio

from .helpers import DummyStreamsContextManager, tee


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_server_allowed_tools(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """Test that if we specified allowed tools, the list of tools is reduced and contains only
    the allowed ones on each call to `list_tools()`.
    """
    allowed_tools = ["tool1", "tool3"]
    server = MCPServerStdio(
        params={
            "command": tee,
        },
        cache_tools_list=True,
        allowed_tools=allowed_tools
    )

    all_tools = [
        MCPTool(name="tool1", inputSchema={}),
        MCPTool(name="tool2", inputSchema={}),
        MCPTool(name="tool3", inputSchema={}),
        MCPTool(name="tool4", inputSchema={}),
    ]

    mock_list_tools.return_value = ListToolsResult(tools=all_tools)

    async with server:
        tools = await server.list_tools()

        # Check it returns only the number of allowed tools
        assert len(tools) == len(allowed_tools)
        # Check it returns exactly only the allowed tools
        assert {tool.name for tool in tools} == set(allowed_tools)

        # Call list_tools() again, should use cached filtered results
        tools = await server.list_tools()
        assert len(tools) == len(allowed_tools)
        assert {tool.name for tool in tools} == set(allowed_tools)

        # Invalidate cache and verify filtering still works
        server.invalidate_tools_cache()
        tools = await server.list_tools()
        assert len(tools) == len(allowed_tools)
        assert {tool.name for tool in tools} == set(allowed_tools)
