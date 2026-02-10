from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import ListToolsResult, Tool as MCPTool

from agents.mcp import MCPServerStdio

from .helpers import DummyStreamsContextManager, tee


class CountingStreamsContextManager:
    def __init__(self, counter: dict[str, int]):
        self.counter = counter

    async def __aenter__(self):
        self.counter["enter"] += 1
        return (object(), object())

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.counter["exit"] += 1


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
@patch("agents.mcp.server.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("agents.mcp.server.stdio_client")
async def test_cleanup_resets_exit_stack_and_reconnects(
    mock_stdio_client: AsyncMock, mock_initialize: AsyncMock
):
    counter = {"enter": 0, "exit": 0}
    mock_stdio_client.side_effect = lambda params: CountingStreamsContextManager(counter)

    server = MCPServerStdio(
        params={
            "command": tee,
        },
        cache_tools_list=True,
    )

    await server.connect()
    original_exit_stack = server.exit_stack

    await server.cleanup()
    assert server.session is None
    assert server.exit_stack is not original_exit_stack
    assert server.server_initialize_result is None
    assert counter == {"enter": 1, "exit": 1}

    await server.connect()
    await server.cleanup()
    assert counter == {"enter": 2, "exit": 2}
