from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agents.mcp import MCPServerStdio
from agents.mcp._compat import MCP_V2, result_is_error

LEGACY_SERVER_PATH = Path(__file__).parent / "servers" / "legacy.py"


@pytest.mark.asyncio
async def test_stdio_connects_to_legacy_server():
    server = MCPServerStdio(
        name="legacy-test-server",
        params={"command": sys.executable, "args": [str(LEGACY_SERVER_PATH)]},
    )

    async with server:
        tools = await server.list_tools()
        result = await server.call_tool("legacy_tool", {})
        protocol_version = getattr(server.session, "protocol_version", None)

    assert [tool.name for tool in tools] == ["legacy_tool"]
    assert result.content[0].type == "text"
    assert result_is_error(result) is False
    if MCP_V2:
        assert protocol_version == "2025-06-18"
        assert server.server_initialize_result is not None
