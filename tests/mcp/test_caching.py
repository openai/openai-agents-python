from unittest.mock import AsyncMock, call, patch

import pytest
from mcp.types import PaginatedRequestParams

from agents import Agent
from agents.mcp import MCPServerStdio
from agents.run_context import RunContextWrapper

from .helpers import DummyStreamsContextManager, tee
from .model_compat import ListToolsResult, Tool as MCPTool


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_server_caching_works(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """Test that if we turn caching on, the list of tools is cached and not fetched from the server
    on each call to `list_tools()`.
    """
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

    async with server:
        # Create test context and agent
        run_context = RunContextWrapper(context=None)
        agent = Agent(name="test_agent", instructions="Test agent")

        # Call list_tools() multiple times
        result_tools = await server.list_tools(run_context, agent)
        assert result_tools == tools

        assert mock_list_tools.call_count == 1, "list_tools() should have been called once"

        # Call list_tools() again, should return the cached value
        result_tools = await server.list_tools(run_context, agent)
        assert result_tools == tools

        assert mock_list_tools.call_count == 1, "list_tools() should not have been called again"

        # Invalidate the cache and call list_tools() again
        server.invalidate_tools_cache()
        result_tools = await server.list_tools(run_context, agent)
        assert result_tools == tools

        assert mock_list_tools.call_count == 2, "list_tools() should be called again"

        # Without invalidating the cache, calling list_tools() again should return the cached value
        result_tools = await server.list_tools(run_context, agent)
        assert result_tools == tools


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_paginated_tools_are_cached_before_filtering(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    first_page_tool = MCPTool(name="first_page_tool", inputSchema={})
    second_page_tool = MCPTool(name="second_page_tool", inputSchema={})
    mock_list_tools.side_effect = [
        ListToolsResult(tools=[first_page_tool], nextCursor=""),
        ListToolsResult(tools=[second_page_tool]),
    ]
    server = MCPServerStdio(
        params={"command": tee},
        cache_tools_list=True,
        tool_filter={"allowed_tool_names": ["second_page_tool"]},
    )

    async with server:
        filtered_tools = await server.list_tools()
        cached_tools = server.cached_tools
        filtered_tools_again = await server.list_tools()

    assert filtered_tools == [second_page_tool]
    assert filtered_tools_again == [second_page_tool]
    assert cached_tools == [first_page_tool, second_page_tool]
    assert mock_list_tools.await_args_list == [
        call(),
        call(params=PaginatedRequestParams(cursor="")),
    ]
