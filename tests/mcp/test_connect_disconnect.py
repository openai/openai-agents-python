from contextlib import AsyncExitStack
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


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
async def test_cleanup_resets_state_for_reconnection(mock_initialize: AsyncMock, mock_stdio_client):
    """Test that cleanup resets all session state so the same instance can reconnect."""
    server = MCPServerStdio(
        params={"command": tee},
        cache_tools_list=True,
    )

    await server.connect()
    first_exit_stack = server.exit_stack
    assert server.session is not None
    assert server.server_initialize_result is not None or mock_initialize.return_value is None

    await server.cleanup()

    # All session state must be cleared
    assert server.session is None
    assert server.server_initialize_result is None
    assert server._get_session_id is None
    # Exit stack must be a fresh instance so a subsequent connect() works
    assert isinstance(server.exit_stack, AsyncExitStack)
    assert server.exit_stack is not first_exit_stack


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
@patch("mcp.client.session.ClientSession.list_tools")
async def test_reconnect_after_cleanup(
    mock_list_tools: AsyncMock, mock_initialize: AsyncMock, mock_stdio_client
):
    """Test that an MCPServerStdio instance can reconnect after cleanup."""
    server = MCPServerStdio(
        params={"command": tee},
        cache_tools_list=True,
    )

    tools = [MCPTool(name="tool1", inputSchema={})]
    mock_list_tools.return_value = ListToolsResult(tools=tools)

    # First connection cycle
    await server.connect()
    result = await server.list_tools()
    assert len(result) == 1
    await server.cleanup()
    assert server.session is None

    # Second connection cycle on the same instance
    await server.connect()
    assert server.session is not None
    result = await server.list_tools()
    assert len(result) == 1
    await server.cleanup()
    assert server.session is None


@pytest.mark.asyncio
@patch("mcp.client.stdio.stdio_client", return_value=DummyStreamsContextManager())
@patch("mcp.client.session.ClientSession.initialize", new_callable=AsyncMock, return_value=None)
async def test_cleanup_closes_write_stream_before_exit_stack(
    mock_initialize: AsyncMock, mock_stdio_client
):
    """Test that cleanup closes the session write stream before unwinding the exit stack.

    This ordering ensures the subprocess receives EOF on stdin and can shut down
    gracefully (releasing multiprocessing semaphores, etc.) before task-group
    cancellation kills the reader/writer coroutines inside the transport.
    """
    server = MCPServerStdio(
        params={"command": tee},
    )

    await server.connect()
    assert server.session is not None

    # Track the order of operations during cleanup
    call_order: list[str] = []
    original_aclose = server.session._write_stream.aclose

    async def tracked_write_stream_close():
        call_order.append("write_stream_closed")
        return await original_aclose()

    original_exit_stack_aclose = server.exit_stack.aclose

    async def tracked_exit_stack_aclose():
        call_order.append("exit_stack_closed")
        return await original_exit_stack_aclose()

    server.session._write_stream.aclose = tracked_write_stream_close  # type: ignore[assignment]
    server.exit_stack.aclose = tracked_exit_stack_aclose  # type: ignore[assignment]

    await server.cleanup()

    # The write stream may be closed multiple times (our explicit close, then again
    # during exit_stack unwind by ClientSession.__aexit__).  The critical invariant
    # is that the FIRST close happens before the exit_stack unwind begins.
    assert len(call_order) >= 2, f"Expected at least 2 calls, got: {call_order}"
    assert call_order[0] == "write_stream_closed", (
        f"Write stream must be closed first, got: {call_order}"
    )
    assert call_order[1] == "exit_stack_closed", (
        f"Exit stack must be closed after write stream, got: {call_order}"
    )
