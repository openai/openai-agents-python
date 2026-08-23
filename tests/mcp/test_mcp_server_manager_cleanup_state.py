import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from agents.mcp import MCPServer, MCPServerManager


@pytest.mark.asyncio
async def test_cleanup_all_removes_cleaned_servers_from_active_servers() -> None:
    server = cast(MCPServer, Mock(spec=MCPServer))
    server.connect = AsyncMock()
    server.cleanup = AsyncMock()

    manager = MCPServerManager([server])
    assert await manager.connect_all() == [server]

    await manager.cleanup_all()

    assert manager.active_servers == []
    assert manager._connected_servers == set()

    assert await manager.reconnect() == []
    assert manager.active_servers == []
    assert server.connect.await_count == 1

    assert await manager.connect_all() == [server]
    assert server.connect.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_all_removes_cleaned_servers_from_active_servers_when_keeping_failed() -> (
    None
):
    """`drop_failed_servers=False` keeps connect-phase failures listed, but a server that was
    cleaned up is disconnected and must not remain advertised as active."""
    server = cast(MCPServer, Mock(spec=MCPServer))
    server.connect = AsyncMock()
    server.cleanup = AsyncMock()

    manager = MCPServerManager([server], drop_failed_servers=False)
    assert await manager.connect_all() == [server]

    await manager.cleanup_all()

    assert manager.active_servers == []
    assert manager._connected_servers == set()


@pytest.mark.asyncio
async def test_manager_owns_repeated_server_instance_once() -> None:
    server = cast(MCPServer, Mock(spec=MCPServer))
    server.connect = AsyncMock()
    server.cleanup = AsyncMock()

    manager = MCPServerManager([server, server])

    assert manager.all_servers == [server]
    assert await manager.connect_all() == [server]
    await manager.cleanup_all()

    server.connect.assert_awaited_once()
    server.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_all_refreshes_active_servers_when_cancellation_propagates() -> None:
    server = cast(MCPServer, Mock(spec=MCPServer))
    server.connect = AsyncMock()
    server.cleanup = AsyncMock(side_effect=asyncio.CancelledError)

    manager = MCPServerManager([server], suppress_cancelled_error=False)
    assert await manager.connect_all() == [server]

    with pytest.raises(asyncio.CancelledError):
        await manager.cleanup_all()

    assert manager.active_servers == []
    assert manager._connected_servers == set()
