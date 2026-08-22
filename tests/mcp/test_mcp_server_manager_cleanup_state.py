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

    assert await manager.connect_all() == [server]
    assert server.connect.await_count == 2
